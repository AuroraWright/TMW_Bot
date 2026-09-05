import asyncio
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import yaml

from cogs.message_count_roles import (
    MessageCountRoleRule,
    MessageCountRoles,
    MessageCountRoleSettings,
)

SOURCE_GUILD_ID = 1
DESTINATION_GUILD_ID = 2
DESTINATION_ROLE_ID = 20
USER_ID = 100


def make_message(
    message_id, channel, *, user_id=USER_ID, bot=False, guild_id=SOURCE_GUILD_ID
):
    return SimpleNamespace(
        id=message_id,
        guild=SimpleNamespace(id=guild_id),
        channel=channel,
        author=SimpleNamespace(id=user_id, bot=bot),
    )


def make_history_channel(channel_id, messages, *, parent_id=None, channel_type=0):
    async def history(**kwargs):
        after = kwargs.get("after")
        after_id = getattr(after, "id", 0) if after is not None else 0
        for message in messages:
            if message.id > after_id:
                yield message

    return SimpleNamespace(
        id=channel_id,
        parent_id=parent_id,
        type=SimpleNamespace(value=channel_type),
        history=history,
    )


class MessageCountRoleSettingsTests(unittest.TestCase):
    def test_production_rule_is_configured_without_python_ids(self):
        config = yaml.safe_load(
            Path("config/message_count_roles.yml").read_text(encoding="utf-8")
        )
        rule = config["rules"][0]

        self.assertEqual(rule["source_guild_id"], 617136488840429598)
        self.assertEqual(rule["destination_guild_id"], 1545162007354024040)
        self.assertEqual(rule["destination_role_id"], 1545600348033384469)
        self.assertEqual(rule["message_threshold"], 10)
        self.assertEqual(config["auto_receive_interval_minutes"], 15)
        self.assertEqual(config["history_scan_worker_count"], 2)
        self.assertEqual(config["award_worker_count"], 2)
        self.assertEqual(
            rule["excluded_channel_ids"],
            [1027706916874506311, 1345775806399647907, 814947177608118273],
        )

    def test_rules_can_be_configured_as_a_mapping(self):
        settings = MessageCountRoleSettings.from_mapping(
            {
                "rules": {
                    "chat": {
                        "source_guild_id": 1,
                        "destination_guild_id": 2,
                        "destination_role_id": 3,
                        "message_threshold": 5,
                    }
                }
            }
        )

        self.assertEqual(settings.rules[0].name, "chat")
        self.assertEqual(settings.rules[0].message_threshold, 5)


class MessageCountRolesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.rule = MessageCountRoleRule(
            name="test-rule",
            source_guild_id=SOURCE_GUILD_ID,
            destination_guild_id=DESTINATION_GUILD_ID,
            destination_role_id=DESTINATION_ROLE_ID,
            message_threshold=2,
            excluded_channel_ids=frozenset({99}),
        )
        self.settings = MessageCountRoleSettings(
            enabled=True,
            retroactive_scan=True,
            monitor_new_messages=True,
            reconcile_interval_minutes=30,
            history_batch_size=2,
            history_batch_delay_seconds=0,
            rules=(self.rule,),
        )
        self.destination_role = SimpleNamespace(
            id=DESTINATION_ROLE_ID,
            managed=False,
            is_assignable=Mock(return_value=True),
        )
        self.destination_member = SimpleNamespace(
            id=USER_ID,
            bot=False,
            guild=None,
            roles=[],
            add_roles=AsyncMock(),
        )
        self.destination_guild = SimpleNamespace(
            id=DESTINATION_GUILD_ID,
            members=[self.destination_member],
            get_role=Mock(return_value=self.destination_role),
            get_member=Mock(return_value=self.destination_member),
        )
        self.destination_member.guild = self.destination_guild
        self.source_guild = SimpleNamespace(id=SOURCE_GUILD_ID, channels=[], threads=[])
        self.bot = Mock()
        self.bot.RUN = AsyncMock()
        self.bot.RUN_MANY = AsyncMock()
        self.bot.GET = AsyncMock(return_value=[])
        self.bot.get_guild.side_effect = {
            SOURCE_GUILD_ID: self.source_guild,
            DESTINATION_GUILD_ID: self.destination_guild,
        }.get
        self.cog = MessageCountRoles(self.bot, self.settings)

    async def test_live_messages_award_destination_role_at_threshold(self):
        channel = SimpleNamespace(id=10, parent_id=None)

        await self.cog.on_message(make_message(1, channel))
        self.destination_member.add_roles.assert_not_awaited()

        await self.cog.on_message(make_message(2, channel))

        self.destination_member.add_roles.assert_awaited_once_with(
            self.destination_role,
            reason="Reached the configured message-count role threshold",
        )
        self.assertEqual(self.cog._counts[self.rule.name][USER_ID], 2)

    async def test_live_threshold_fetches_destination_member_when_cache_misses(self):
        uncached_member = SimpleNamespace(
            id=USER_ID,
            bot=False,
            guild=self.destination_guild,
            roles=[],
            add_roles=AsyncMock(),
        )
        self.destination_guild.get_member.return_value = None
        self.destination_guild.fetch_member = AsyncMock(return_value=uncached_member)
        channel = SimpleNamespace(id=10, parent_id=None)

        await self.cog.on_message(make_message(1, channel))
        await self.cog.on_message(make_message(2, channel))

        uncached_member.add_roles.assert_awaited_once_with(
            self.destination_role,
            reason="Reached the configured message-count role threshold",
        )

    async def test_excluded_channels_threads_and_bot_messages_do_not_count(self):
        excluded_channel = SimpleNamespace(id=99, parent_id=None)
        excluded_thread = SimpleNamespace(
            id=100,
            parent_id=99,
            type=SimpleNamespace(value=11),
        )
        normal_channel = SimpleNamespace(id=10, parent_id=None)

        await self.cog.on_message(make_message(1, excluded_channel))
        await self.cog.on_message(make_message(2, excluded_thread))
        await self.cog.on_message(make_message(3, normal_channel, bot=True))

        self.assertNotIn(USER_ID, self.cog._counts[self.rule.name])
        self.destination_member.add_roles.assert_not_awaited()

    async def test_channel_under_an_excluded_category_is_not_excluded(self):
        channel = SimpleNamespace(
            id=10,
            parent_id=99,
            type=SimpleNamespace(value=0),
        )

        for message_id in (1, 2):
            await self.cog.on_message(make_message(message_id, channel))

        self.destination_member.add_roles.assert_awaited_once()

    async def test_retroactive_scan_backfills_existing_destination_members(self):
        channel = make_history_channel(
            10,
            [
                make_message(1, SimpleNamespace(id=10, parent_id=None)),
                make_message(2, SimpleNamespace(id=10, parent_id=None)),
            ],
        )
        self.source_guild.channels = [channel]

        await self.cog.reconcile_all()

        self.destination_member.add_roles.assert_awaited_once_with(
            self.destination_role,
            reason="Reached the configured message-count role threshold",
        )
        self.assertEqual(self.cog._scan_cursors[self.rule.name][channel.id], 2)

    async def test_archived_forum_threads_are_discovered(self):
        thread_message_channel = SimpleNamespace(id=31, parent_id=30)
        archived_thread = make_history_channel(
            31,
            [
                make_message(1, thread_message_channel),
                make_message(2, thread_message_channel),
            ],
            parent_id=30,
            channel_type=11,
        )

        async def archived_threads(**kwargs):
            self.assertEqual(kwargs, {"limit": None})
            yield archived_thread

        forum_parent = SimpleNamespace(
            id=30,
            type=SimpleNamespace(value=15),
            archived_threads=archived_threads,
        )
        self.source_guild.channels = [forum_parent]

        await self.cog.reconcile_all()

        self.destination_member.add_roles.assert_awaited_once()

    async def test_channel_discovery_refreshes_the_source_guild_cache(self):
        refreshed_channel = make_history_channel(40, [])

        async def fetch_channels():
            return [refreshed_channel]

        self.source_guild.fetch_channels = fetch_channels

        channels = await self.cog._message_channels(self.rule, self.source_guild)

        self.assertEqual([channel.id for channel in channels], [40])

    async def test_all_private_archived_threads_are_discovered_with_manage_threads(self):
        calls = []
        public_thread = make_history_channel(
            51,
            [],
            parent_id=50,
            channel_type=11,
        )
        private_thread = make_history_channel(
            52,
            [],
            parent_id=50,
            channel_type=12,
        )

        async def archived_threads(**kwargs):
            calls.append(kwargs)
            if kwargs == {"private": False, "limit": None}:
                yield public_thread
            elif kwargs == {
                "private": True,
                "joined": False,
                "limit": None,
            }:
                yield private_thread
            else:
                self.fail(f"Unexpected archived-thread request: {kwargs}")

        parent = SimpleNamespace(
            id=50,
            type=SimpleNamespace(value=0),
            archived_threads=archived_threads,
            permissions_for=Mock(
                return_value=SimpleNamespace(manage_threads=True)
            ),
        )
        self.source_guild.channels = [parent]
        self.source_guild.me = SimpleNamespace(id=999)

        channels = await self.cog._message_channels(self.rule, self.source_guild)

        self.assertEqual([channel.id for channel in channels], [51, 52])
        self.assertEqual(
            calls,
            [
                {"private": False, "limit": None},
                {"private": True, "joined": False, "limit": None},
            ],
        )

    async def test_scan_resumes_after_saved_cursor(self):
        history_calls = []
        messages = [make_message(3, SimpleNamespace(id=10, parent_id=None))]

        async def history(**kwargs):
            history_calls.append(kwargs)
            for message in messages:
                yield message

        channel = SimpleNamespace(id=10, history=history)
        self.cog._scan_cursors[self.rule.name][channel.id] = 2

        await self.cog._scan_channel(self.rule, channel)

        self.assertEqual(history_calls[0]["after"].id, 2)
        self.assertEqual(self.cog._scan_cursors[self.rule.name][channel.id], 3)

    async def test_channel_scans_are_bounded_by_scan_worker_count(self):
        settings = MessageCountRoleSettings(
            enabled=True,
            retroactive_scan=True,
            monitor_new_messages=True,
            reconcile_interval_minutes=30,
            history_scan_worker_count=2,
            rules=(self.rule,),
        )
        cog = MessageCountRoles(self.bot, settings)
        active = 0
        maximum_active = 0
        scanned_ids = []

        async def scan_channel(_rule, channel):
            nonlocal active, maximum_active
            active += 1
            maximum_active = max(maximum_active, active)
            scanned_ids.append(channel.id)
            await asyncio.sleep(0)
            active -= 1

        cog._scan_channel = scan_channel
        channels = [SimpleNamespace(id=index) for index in range(5)]

        await cog._scan_channels(self.rule, channels)

        self.assertEqual(sorted(scanned_ids), list(range(5)))
        self.assertLessEqual(maximum_active, 2)

    async def test_scan_status_reports_failed_histories(self):
        cog = MessageCountRoles(self.bot, self.settings)

        async def scan_channel(_rule, channel):
            return channel.id != 2

        cog._scan_channel = scan_channel
        status = await cog._scan_channels(
            self.rule,
            [SimpleNamespace(id=1), SimpleNamespace(id=2)],
        )

        self.assertEqual(status.discovered_channels, 2)
        self.assertEqual(status.failed_channel_ids, (2,))

    async def test_destination_member_join_uses_loaded_count(self):
        self.cog._counts[self.rule.name][USER_ID] = self.rule.message_threshold

        await self.cog.on_member_join(self.destination_member)

        self.destination_member.add_roles.assert_awaited_once()

    async def test_history_threshold_queues_without_waiting_for_role_api(self):
        settings = MessageCountRoleSettings(
            enabled=True,
            retroactive_scan=True,
            monitor_new_messages=True,
            reconcile_interval_minutes=30,
            award_worker_count=1,
            rules=(self.rule,),
        )
        cog = MessageCountRoles(self.bot, settings)
        started = asyncio.Event()
        release = asyncio.Event()

        async def award_member(_rule, _member):
            started.set()
            await release.wait()
            return True

        cog._award_member = award_member
        cog._start_award_workers()
        channel = SimpleNamespace(id=10, parent_id=None)

        await cog._record_message(
            self.rule,
            make_message(1, channel),
            from_history=True,
        )
        await cog._record_message(
            self.rule,
            make_message(2, channel),
            from_history=True,
        )

        await started.wait()
        self.assertFalse(release.is_set())
        release.set()
        await cog._award_queue.join()
        await cog._stop_award_workers()

    async def test_destination_member_join_refreshes_persisted_count(self):
        self.bot.GET.return_value = [(self.rule.message_threshold,)]

        await self.cog.on_member_join(self.destination_member)

        self.destination_member.add_roles.assert_awaited_once()
        self.assertEqual(
            self.cog._counts[self.rule.name][USER_ID],
            self.rule.message_threshold,
        )

    async def test_auto_receive_fetches_members_missing_from_destination_cache(self):
        uncached_member = SimpleNamespace(
            id=USER_ID,
            bot=False,
            guild=self.destination_guild,
            roles=[],
            add_roles=AsyncMock(),
        )
        self.destination_guild.members = []

        async def fetch_members(*, limit):
            self.assertIsNone(limit)
            yield uncached_member

        self.destination_guild.fetch_members = fetch_members
        self.cog._counts[self.rule.name][USER_ID] = self.rule.message_threshold

        await self.cog.auto_receive_all()

        uncached_member.add_roles.assert_awaited_once_with(
            self.destination_role,
            reason="Reached the configured message-count role threshold",
        )

    async def test_award_workers_prioritize_live_requests_over_backfill(self):
        settings = MessageCountRoleSettings(
            enabled=True,
            retroactive_scan=True,
            monitor_new_messages=True,
            reconcile_interval_minutes=30,
            award_worker_count=1,
            rules=(self.rule,),
        )
        cog = MessageCountRoles(self.bot, settings)
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        processed_ids = []

        async def award_member(_rule, member):
            processed_ids.append(member.id)
            if member.id == 1:
                first_started.set()
                await release_first.wait()
            return True

        cog._award_member = award_member
        cog._start_award_workers()
        members = [SimpleNamespace(id=index, bot=False) for index in (1, 2, 3)]
        first = asyncio.create_task(
            cog._award_member_safely(
                self.rule,
                members[0],
                priority=cog._AWARD_PRIORITY_BACKFILL,
                source="backfill",
            )
        )
        await first_started.wait()
        second = asyncio.create_task(
            cog._award_member_safely(
                self.rule,
                members[1],
                priority=cog._AWARD_PRIORITY_BACKFILL,
                source="backfill",
            )
        )
        third = asyncio.create_task(
            cog._award_member_safely(
                self.rule,
                members[2],
                priority=cog._AWARD_PRIORITY_LIVE,
                source="member-join",
            )
        )
        await asyncio.sleep(0)
        release_first.set()

        self.assertEqual(await first, True)
        self.assertEqual(await third, True)
        self.assertEqual(await second, True)
        self.assertEqual(processed_ids, [1, 3, 2])
        await cog._stop_award_workers()

    async def test_duplicate_awards_share_one_queued_request(self):
        settings = MessageCountRoleSettings(
            enabled=True,
            retroactive_scan=True,
            monitor_new_messages=True,
            reconcile_interval_minutes=30,
            award_worker_count=1,
            rules=(self.rule,),
        )
        cog = MessageCountRoles(self.bot, settings)
        award_member = AsyncMock(return_value=True)
        cog._award_member = award_member
        cog._start_award_workers()
        member = SimpleNamespace(id=USER_ID, bot=False)

        results = await asyncio.gather(
            cog._award_member_safely(self.rule, member, source="member-join"),
            cog._award_member_safely(self.rule, member, source="member-update"),
        )

        self.assertEqual(results, [True, True])
        award_member.assert_awaited_once_with(self.rule, member)
        await cog._stop_award_workers()


if __name__ == "__main__":
    unittest.main()
