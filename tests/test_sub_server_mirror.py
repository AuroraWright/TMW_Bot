import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, call

import discord
import yaml

from cogs.sub_server_access import SubServerSettings
from cogs.sub_server_mirror import (
    CREATE_MIRROR_ENTITIES_TABLE,
    MIRROR_REASON,
    MirrorSettings,
    SubServerMirror,
)

MAIN_GUILD_ID = 1
SUB_GUILD_ID = 2
MEMBER_ID = 100


def make_role(
    role_id,
    *,
    default=False,
    managed=False,
    assignable=True,
    position=1,
):
    role = SimpleNamespace(
        id=role_id,
        managed=managed,
        position=position,
        is_default=Mock(return_value=default),
        is_assignable=Mock(return_value=assignable),
    )
    return role


def make_emoji(emoji_id, name, *, animated=False):
    return SimpleNamespace(
        id=emoji_id,
        name=name,
        animated=animated,
        roles=[],
        read=AsyncMock(return_value=b"image"),
        edit=AsyncMock(),
        delete=AsyncMock(),
    )


class MirrorSettingsTests(unittest.TestCase):
    def test_configured_guild_ids_and_destructive_scope_are_explicit(self):
        config = yaml.safe_load(
            Path("config/sub_server_settings.yml").read_text(encoding="utf-8")
        )

        self.assertEqual(config["main_guild_id"], 617136488840429598)
        self.assertEqual(config["sub_guild_ids"], [1545132788997427260])
        self.assertNotIn("required_role_id", config)
        self.assertTrue(config["mirror"]["enabled"])
        self.assertTrue(config["mirror"]["delete_unmapped_roles"])
        self.assertTrue(config["mirror"]["delete_unmapped_channels"])
        self.assertTrue(config["mirror"]["delete_unmapped_emojis"])
        self.assertEqual(config["mirror"]["emoji_creations_per_reconcile"], 5)
        self.assertFalse(config["mirror"]["mirror_guild_name"])

    def test_mirror_settings_validate_types_and_intervals(self):
        settings = MirrorSettings.from_mapping(
            {
                "enabled": True,
                "reconcile_interval_minutes": "15",
                "event_debounce_seconds": 2,
            }
        )

        self.assertTrue(settings.enabled)
        self.assertEqual(settings.reconcile_interval_minutes, 15.0)
        self.assertEqual(settings.event_debounce_seconds, 2.0)

        with self.assertRaises(TypeError):
            MirrorSettings.from_mapping({"enabled": "yes"})
        with self.assertRaises(ValueError):
            MirrorSettings.from_mapping({"reconcile_interval_minutes": 0})
        with self.assertRaises(TypeError):
            MirrorSettings.from_mapping({"emoji_creations_per_reconcile": 1.5})
        with self.assertRaises(TypeError):
            MirrorSettings.from_mapping({"mirror_guild_name": "no"})

    def test_production_guild_ids_are_not_hardcoded_in_python(self):
        source = "\n".join(
            path.read_text(encoding="utf-8")
            for directory in ("cogs", "lib")
            for path in Path(directory).glob("*.py")
        )

        self.assertNotIn("617136488840429598", source)
        self.assertNotIn("1545132788997427260", source)


class SubServerMirrorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = Mock()
        self.bot.RUN = AsyncMock()
        self.bot.GET = AsyncMock(return_value=[])
        self.bot.user = SimpleNamespace(id=999)
        self.cog = SubServerMirror(
            self.bot,
            MirrorSettings(
                enabled=True,
                mutation_delay_seconds=0,
                emoji_create_delay_seconds=0,
                delete_unmapped_roles=True,
                delete_unmapped_channels=True,
                delete_unmapped_emojis=True,
            ),
        )
        self.cog.access_settings = SubServerSettings(
            main_guild_id=MAIN_GUILD_ID,
            sub_guild_ids=(SUB_GUILD_ID,),
        )

    async def test_cog_load_creates_mapping_table(self):
        await self.cog.cog_load()

        self.bot.RUN.assert_awaited_once_with(CREATE_MIRROR_ENTITIES_TABLE)

    def test_direction_guard_rejects_writes_to_main_guild(self):
        main = SimpleNamespace(id=MAIN_GUILD_ID)
        sub = SimpleNamespace(id=SUB_GUILD_ID)

        self.cog._validate_direction(main, sub)
        with self.assertRaises(RuntimeError):
            self.cog._validate_direction(sub, main)
        with self.assertRaises(RuntimeError):
            self.cog._validate_direction(main, main)

    async def test_reconciliation_is_strictly_source_to_destination(self):
        source = SimpleNamespace(id=MAIN_GUILD_ID, edit=AsyncMock())
        destination = SimpleNamespace(id=SUB_GUILD_ID)
        role_map = {10: object()}
        emoji_map = {20: SimpleNamespace(id=200)}
        channel_map = {30: object()}
        self.cog._sync_roles = AsyncMock(return_value=role_map)
        self.cog._sync_emojis = AsyncMock(return_value=emoji_map)
        self.cog._existing_emoji_map = AsyncMock(return_value=emoji_map)
        self.cog._sync_channels = AsyncMock(return_value=channel_map)
        self.cog._ensure_destination_community = AsyncMock(
            return_value=(destination, False)
        )
        self.cog._sync_guild_settings = AsyncMock()
        self.cog._sync_all_member_roles = AsyncMock()

        await self.cog._reconcile_guild(source, destination)

        self.cog._sync_roles.assert_awaited_once_with(source, destination)
        self.cog._sync_emojis.assert_awaited_once_with(source, destination, role_map)
        self.cog._existing_emoji_map.assert_awaited_once_with(destination)
        self.cog._sync_channels.assert_awaited_once_with(
            source, destination, role_map, emoji_map
        )
        self.cog._sync_guild_settings.assert_awaited_once_with(
            source, destination, channel_map
        )
        source.edit.assert_not_awaited()

    async def test_events_during_reconciliation_queue_a_follow_up_pass(self):
        self.cog.settings = MirrorSettings(
            enabled=True,
            event_debounce_seconds=0,
            mutation_delay_seconds=0,
            emoji_create_delay_seconds=0,
        )
        calls = 0

        async def reconcile():
            nonlocal calls
            calls += 1
            if calls == 1:
                self.cog._schedule_reconcile()

        self.cog.reconcile_all = AsyncMock(side_effect=reconcile)

        self.cog._schedule_reconcile()
        await self.cog._debounced_reconcile

        self.assertEqual(self.cog.reconcile_all.await_count, 2)

    def test_destination_events_during_reconciliation_do_not_queue_a_pass(self):
        self.cog._active_destination_ids.add(SUB_GUILD_ID)

        self.cog._schedule_reconcile(SUB_GUILD_ID)

        self.assertIsNone(self.cog._debounced_reconcile)

    async def test_member_roles_are_replaced_from_main_guild_authority(self):
        source_default = make_role(MAIN_GUILD_ID, default=True)
        source_keep = make_role(10)
        source_member = SimpleNamespace(
            id=MEMBER_ID,
            roles=[source_default, source_keep],
        )
        destination_default = make_role(SUB_GUILD_ID, default=True)
        destination_keep = make_role(20)
        destination_remove = make_role(30)
        destination_member = SimpleNamespace(
            id=MEMBER_ID,
            guild=SimpleNamespace(id=SUB_GUILD_ID),
            roles=[destination_default, destination_remove],
            add_roles=AsyncMock(),
            remove_roles=AsyncMock(),
        )

        await self.cog._sync_member_roles_unlocked(
            source_member,
            destination_member,
            {
                MAIN_GUILD_ID: destination_default,
                source_keep.id: destination_keep,
                11: destination_remove,
            },
        )

        destination_member.add_roles.assert_awaited_once_with(
            destination_keep, reason=MIRROR_REASON
        )
        destination_member.remove_roles.assert_awaited_once_with(
            destination_remove, reason=MIRROR_REASON
        )

    def test_role_positions_preserve_relative_order_across_managed_role_gaps(self):
        source_low = make_role(10, position=1)
        source_managed = make_role(11, managed=True, position=5)
        source_middle = make_role(12, position=8)
        source_high = make_role(13, position=20)

        def destination_role(role_id, position, *, managed=False):
            role = Mock(id=role_id, position=position, managed=managed)
            role.is_assignable.return_value = not managed
            return role

        destination_low = destination_role(20, 3)
        destination_managed = destination_role(21, 4, managed=True)
        destination_middle = destination_role(22, 2)
        destination_high = destination_role(23, 1)

        positions = self.cog._mirrored_role_positions(
            [source_high, source_low, source_middle, source_managed],
            {
                source_low.id: destination_low,
                source_managed.id: destination_managed,
                source_middle.id: destination_middle,
                source_high.id: destination_high,
            },
        )

        self.assertEqual(
            positions,
            {
                destination_low: 1,
                destination_high: 3,
            },
        )

    async def test_role_moves_never_include_the_bot_managed_role(self):
        movable = Mock(id=20, position=2, managed=False)
        movable.is_assignable.return_value = True
        bot_role = Mock(id=21, position=4, managed=True)
        bot_role.is_assignable.return_value = False
        destination = SimpleNamespace(
            get_role=Mock(
                side_effect=lambda role_id: movable if role_id == 20 else bot_role
            ),
            edit_role_positions=AsyncMock(),
            me=SimpleNamespace(top_role=bot_role),
        )

        await self.cog._apply_mirrored_role_positions(
            destination,
            {movable: 1, bot_role: 3},
        )

        destination.edit_role_positions.assert_awaited_once_with(
            positions={movable: 1},
            reason=MIRROR_REASON,
        )

    async def test_channel_positions_use_relative_order_and_are_idempotent(self):
        source_first = SimpleNamespace(id=10, position=4, category_id=None)
        source_second = SimpleNamespace(id=11, position=19, category_id=None)
        channel_type = SimpleNamespace(value=0)
        destination_first = SimpleNamespace(
            id=20,
            position=0,
            category_id=None,
            _sorting_bucket=0,
            type=channel_type,
        )
        destination_second = SimpleNamespace(
            id=21,
            position=1,
            category_id=None,
            _sorting_bucket=0,
            type=channel_type,
        )
        http = SimpleNamespace(bulk_channel_update=AsyncMock())
        destination = SimpleNamespace(
            id=SUB_GUILD_ID,
            _state=SimpleNamespace(http=http),
        )

        await self.cog._sync_channel_positions(
            [source_second, source_first],
            destination,
            {
                source_first.id: destination_first,
                source_second.id: destination_second,
            },
        )

        http.bulk_channel_update.assert_not_awaited()

        destination_first.position = 1
        destination_second.position = 0
        await self.cog._sync_channel_positions(
            [source_second, source_first],
            destination,
            {
                source_first.id: destination_first,
                source_second.id: destination_second,
            },
        )

        http.bulk_channel_update.assert_awaited_once_with(
            SUB_GUILD_ID,
            [
                {"id": destination_first.id, "position": 0},
                {"id": destination_second.id, "position": 1},
            ],
            reason=MIRROR_REASON,
        )

    async def test_existing_emoji_is_adopted_before_unmapped_cleanup(self):
        source_emoji = make_emoji(10, "same")
        destination_emoji = make_emoji(20, "same")
        source = SimpleNamespace(id=MAIN_GUILD_ID, emojis=[source_emoji])
        destination = SimpleNamespace(
            id=SUB_GUILD_ID,
            emojis=[destination_emoji],
            emoji_limit=1,
            get_emoji=Mock(return_value=None),
            create_custom_emoji=AsyncMock(),
        )
        self.cog._load_entity_map = AsyncMock(return_value={})
        self.cog._save_entity_mapping = AsyncMock()
        self.cog._delete_entity_mapping = AsyncMock()

        result = await self.cog._sync_emojis(source, destination, {})

        self.assertIs(result[source_emoji.id], destination_emoji)
        destination.create_custom_emoji.assert_not_awaited()
        destination_emoji.delete.assert_not_awaited()
        self.cog._save_entity_mapping.assert_awaited_once_with(
            SUB_GUILD_ID, "emoji", source_emoji.id, destination_emoji.id
        )

    async def test_forum_without_optional_defaults_can_be_created(self):
        source = SimpleNamespace(
            id=10,
            name="forum",
            type=discord.ChannelType.forum,
            position=1,
            topic=None,
            slowmode_delay=0,
            nsfw=False,
            default_auto_archive_duration=1440,
            default_thread_slowmode_delay=0,
            default_sort_order=None,
            default_reaction_emoji=None,
            default_layout=discord.ForumLayoutType.not_set,
            available_tags=[],
        )
        created = SimpleNamespace(id=20)
        destination = SimpleNamespace(create_forum=AsyncMock(return_value=created))

        result = await self.cog._create_channel(
            source,
            destination,
            category=None,
            overwrites={},
            emoji_map={},
        )

        self.assertIs(result, created)
        kwargs = destination.create_forum.await_args.kwargs
        self.assertNotIn("default_sort_order", kwargs)
        self.assertNotIn("default_reaction_emoji", kwargs)
        self.assertEqual(kwargs["default_layout"], discord.ForumLayoutType.not_set)

    async def test_community_enable_keeps_connected_destination_cache(self):
        rules = SimpleNamespace(id=10)
        updates = SimpleNamespace(id=11)
        source = SimpleNamespace(
            features=["COMMUNITY"],
            rules_channel=rules,
            public_updates_channel=updates,
            verification_level=discord.VerificationLevel.none,
            explicit_content_filter=discord.ContentFilter.disabled,
        )
        detached_result = SimpleNamespace(id=SUB_GUILD_ID, channels=[])
        destination = SimpleNamespace(
            id=SUB_GUILD_ID,
            features=[],
            edit=AsyncMock(return_value=detached_result),
        )
        mirrored_rules = SimpleNamespace(id=20)
        mirrored_updates = SimpleNamespace(id=21)

        result, enabled = await self.cog._ensure_destination_community(
            source,
            destination,
            {rules.id: mirrored_rules, updates.id: mirrored_updates},
        )

        self.assertTrue(enabled)
        self.assertIs(result, destination)
        destination.edit.assert_awaited_once_with(
            community=True,
            rules_channel=mirrored_rules,
            public_updates_channel=mirrored_updates,
            verification_level=discord.VerificationLevel.low,
            explicit_content_filter=discord.ContentFilter.all_members,
            reason=MIRROR_REASON,
        )

    async def test_guild_name_can_remain_destination_managed(self):
        self.cog.settings = MirrorSettings(
            enabled=True,
            mirror_guild_settings=True,
            mirror_guild_name=False,
            mutation_delay_seconds=0,
        )
        shared = {
            "features": [],
            "verification_level": discord.VerificationLevel.none,
            "default_notifications": discord.NotificationLevel.all_messages,
            "explicit_content_filter": discord.ContentFilter.disabled,
            "afk_timeout": 300,
            "system_channel_flags": SimpleNamespace(value=0),
            "icon": None,
            "afk_channel": None,
            "system_channel": None,
        }
        source = SimpleNamespace(name="Main server", **shared)
        destination = SimpleNamespace(
            name="My custom backup name",
            edit=AsyncMock(),
            **shared,
        )

        await self.cog._sync_guild_settings(source, destination, {})

        destination.edit.assert_not_awaited()

    def test_optional_role_style_falls_back_to_base_colour(self):
        kwargs = {
            "name": "gradient",
            "colour": discord.Colour.red(),
            "secondary_colour": discord.Colour.blue(),
            "tertiary_colour": None,
            "display_icon": b"icon",
            "reason": MIRROR_REASON,
        }

        fallback = self.cog._without_optional_role_style(kwargs)

        self.assertEqual(
            fallback,
            {
                "name": "gradient",
                "colour": discord.Colour.red(),
                "reason": MIRROR_REASON,
            },
        )

    async def test_missing_emoji_waits_for_a_later_free_slot(self):
        first_source = make_emoji(10, "first")
        waiting_source = make_emoji(11, "waiting")
        first_destination = make_emoji(20, "first")
        created_destination = make_emoji(21, "waiting")
        source = SimpleNamespace(
            id=MAIN_GUILD_ID,
            emojis=[first_source, waiting_source],
        )
        destination = SimpleNamespace(
            id=SUB_GUILD_ID,
            emojis=[first_destination],
            emoji_limit=1,
            get_emoji=Mock(
                side_effect=lambda emoji_id: (
                    first_destination if emoji_id == 20 else None
                )
            ),
            create_custom_emoji=AsyncMock(return_value=created_destination),
        )
        self.cog._load_entity_map = AsyncMock(return_value={10: 20})
        self.cog._save_entity_mapping = AsyncMock()
        self.cog._delete_entity_mapping = AsyncMock()

        await self.cog._sync_emojis(source, destination, {})
        destination.create_custom_emoji.assert_not_awaited()

        destination.emoji_limit = 2
        await self.cog._sync_emojis(source, destination, {})

        destination.create_custom_emoji.assert_awaited_once_with(
            name="waiting",
            image=b"image",
            roles=[],
            reason=MIRROR_REASON,
        )
        self.assertEqual(
            self.cog._save_entity_mapping.await_args_list[-1],
            call(SUB_GUILD_ID, "emoji", waiting_source.id, created_destination.id),
        )

    async def test_emoji_creation_is_bounded_per_reconciliation(self):
        source_emojis = [make_emoji(10 + index, f"emoji_{index}") for index in range(6)]
        created_emojis = [
            make_emoji(20 + index, f"emoji_{index}") for index in range(5)
        ]
        source = SimpleNamespace(id=MAIN_GUILD_ID, emojis=source_emojis)
        destination = SimpleNamespace(
            id=SUB_GUILD_ID,
            emojis=[],
            emoji_limit=50,
            get_emoji=Mock(return_value=None),
            create_custom_emoji=AsyncMock(side_effect=created_emojis),
        )
        self.cog._load_entity_map = AsyncMock(return_value={})
        self.cog._save_entity_mapping = AsyncMock()
        self.cog._delete_entity_mapping = AsyncMock()

        result = await self.cog._sync_emojis(source, destination, {})

        self.assertEqual(destination.create_custom_emoji.await_count, 5)
        self.assertEqual(len(result), 5)


if __name__ == "__main__":
    unittest.main()
