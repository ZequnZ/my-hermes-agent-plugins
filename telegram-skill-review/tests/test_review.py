import asyncio
import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "telegram_skill_review",
    PLUGIN_ROOT / "__init__.py",
    submodule_search_locations=[str(PLUGIN_ROOT)],
)
assert SPEC is not None and SPEC.loader is not None
plugin = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = plugin
SPEC.loader.exec_module(plugin)


def pending_record(content: str = "# Demo\n\nBody\n") -> dict:
    return {
        "id": "abcd1234",
        "subsystem": "skills",
        "action": "create",
        "summary": "create 'demo'",
        "origin": "background_review",
        "created_at": 1.0,
        "payload": {"action": "create", "name": "demo", "content": content},
    }


def test_split_markdown_preserves_exact_content_and_limit():
    content = "---\nname: demo\n---\n\n# Heading\n\n" + ("paragraph text\n\n" * 80)

    parts = plugin.split_markdown(content, max_chars=240)

    assert "".join(parts) == content
    assert all(len(part) <= 240 for part in parts)
    assert len(parts) > 1


def test_split_markdown_divides_an_oversized_line_without_data_loss():
    content = "x" * 601

    parts = plugin.split_markdown(content, max_chars=200)

    assert [len(part) for part in parts] == [200, 200, 200, 1]
    assert "".join(parts) == content


def test_render_rejects_a_message_limit_that_cannot_hold_the_header():
    try:
        plugin.render_proposal_messages(pending_record("body"), max_chars=60)
    except ValueError as exc:
        assert "max_message_chars" in str(exc)
    else:
        raise AssertionError("expected an invalid message budget to fail")


def test_store_claims_each_pending_record_once(tmp_path):
    pending_dir = tmp_path / "pending" / "skills"
    pending_dir.mkdir(parents=True)
    path = pending_dir / "abcd1234.json"
    path.write_text(json.dumps(pending_record()), encoding="utf-8")
    store = plugin.ReviewStore(tmp_path)

    first = store.claim_for_delivery("abcd1234")
    second = store.claim_for_delivery("abcd1234")

    assert first["review"]["delivery_status"] == "delivering"
    assert second is None


def test_store_records_partial_delivery_for_restart_resume(tmp_path):
    pending_dir = tmp_path / "pending" / "skills"
    pending_dir.mkdir(parents=True)
    path = pending_dir / "abcd1234.json"
    path.write_text(json.dumps(pending_record()), encoding="utf-8")
    store = plugin.ReviewStore(tmp_path)

    store.claim_for_delivery("abcd1234")
    store.record_topic("abcd1234", chat_id="17", thread_id="29")
    store.record_content_message("abcd1234", message_id="41")

    record = store.get_pending("abcd1234")
    assert record["review"]["thread_id"] == "29"
    assert record["review"]["content_message_ids"] == ["41"]


def test_delivery_failure_sets_a_future_retry_time(tmp_path):
    pending_dir = tmp_path / "pending" / "skills"
    pending_dir.mkdir(parents=True)
    (pending_dir / "abcd1234.json").write_text(json.dumps(pending_record()), encoding="utf-8")
    store = plugin.ReviewStore(tmp_path)
    store.record_delivery_failure("abcd1234", "network")

    record = store.get_pending("abcd1234")
    assert record["review"]["next_attempt_at"] > record["review"]["failed_at"]
    assert store.claim_for_delivery("abcd1234") is None


def test_pending_listing_ignores_invalid_record_names(tmp_path):
    pending_dir = tmp_path / "pending" / "skills"
    pending_dir.mkdir(parents=True)
    (pending_dir / "bad name.json").write_text("{}", encoding="utf-8")
    (pending_dir / "abcd1234.json").write_text(json.dumps(pending_record()), encoding="utf-8")

    assert plugin.ReviewStore(tmp_path).list_pending_ids() == ["abcd1234"]


def test_render_messages_keeps_skill_content_after_status_updates():
    record = pending_record("# Demo\n\n" + ("body\n" * 100))
    record["profile"] = "dgme"

    messages = plugin.render_proposal_messages(record, max_chars=180)

    assert "".join(message.source for message in messages) == record["payload"]["content"]
    assert all(len(message.markdown) <= 180 for message in messages)
    assert all("Part" in message.markdown for message in messages)
    assert all("**Profile:** `dgme`" in message.markdown for message in messages)


class FakeBot:
    def __init__(self):
        self.calls = []

    async def create_forum_topic(self, **kwargs):
        self.calls.append(("create_forum_topic", kwargs))
        return SimpleNamespace(message_thread_id=29)

    async def do_api_request(self, method, api_kwargs):
        self.calls.append((method, api_kwargs))
        return {"message_id": 40 + len([c for c in self.calls if c[0] == method])}

    async def edit_message_reply_markup(self, **kwargs):
        self.calls.append(("edit_message_reply_markup", kwargs))

    async def edit_forum_topic(self, **kwargs):
        self.calls.append(("edit_forum_topic", kwargs))


class FakeQuery:
    def __init__(self, data: str):
        self.data = data
        self.from_user = SimpleNamespace(id=88, first_name="Z")
        self.message = SimpleNamespace(
            chat_id=17,
            message_thread_id=29,
            message_id=44,
            chat=SimpleNamespace(type="private"),
        )
        self.answers = []

    async def answer(self, text=None):
        self.answers.append(text)


class FakeAdapter:
    def __init__(self):
        self.chat_types = []

    def _is_callback_user_authorized(self, user_id, **kwargs):
        self.chat_types.append(kwargs.get("chat_type"))
        return user_id == "88"


def test_delivery_creates_one_topic_and_keeps_all_skill_parts(tmp_path):
    pending_dir = tmp_path / "pending" / "skills"
    pending_dir.mkdir(parents=True)
    path = pending_dir / "abcd1234.json"
    path.write_text(json.dumps(pending_record("# Demo\n\n" + "body\n" * 100)), encoding="utf-8")
    bot = FakeBot()
    store = plugin.ReviewStore(tmp_path)
    service = plugin.TelegramSkillReviewService(
        bot=bot,
        adapter=FakeAdapter(),
        store=store,
        chat_id="17",
        max_message_chars=180,
        profile_name="dgme",
    )

    asyncio.run(service.deliver_pending("abcd1234"))

    record = store.get_pending("abcd1234")
    assert record["review"]["delivery_status"] == "sent"
    assert len(record["review"]["content_message_ids"]) > 1
    assert record["review"]["control_message_id"]
    rich_calls = [call for call in bot.calls if call[0] == "sendRichMessage"]
    assert all(call[1]["message_thread_id"] == 29 for call in rich_calls)
    assert "dgme" in bot.calls[0][1]["name"]
    assert "**Profile:** `dgme`" in rich_calls[0][1]["rich_message"]["markdown"]
    assert store.get_pending("abcd1234")["profile"] == "dgme"
    callbacks = [
        button["callback_data"]
        for button in rich_calls[-1][1]["reply_markup"]["inline_keyboard"][0]
    ]
    assert callbacks == ["skillreview:a:dgme:abcd1234", "skillreview:r:dgme:abcd1234"]


def test_delivery_leaves_foreground_records_available_for_manual_review(tmp_path):
    pending_dir = tmp_path / "pending" / "skills"
    pending_dir.mkdir(parents=True)
    record = pending_record()
    record["origin"] = "foreground"
    (pending_dir / "abcd1234.json").write_text(json.dumps(record), encoding="utf-8")
    service = plugin.TelegramSkillReviewService(
        bot=FakeBot(),
        adapter=FakeAdapter(),
        store=plugin.ReviewStore(tmp_path),
        chat_id="17",
    )

    asyncio.run(service.deliver_pending("abcd1234"))

    saved = service.store.get_pending("abcd1234")
    assert saved.get("review") is None


def test_approve_callback_applies_without_llm_and_preserves_telegram_messages(tmp_path):
    pending_dir = tmp_path / "pending" / "skills"
    pending_dir.mkdir(parents=True)
    record = pending_record()
    record["review"] = {
        "delivery_status": "sent",
        "chat_id": "17",
        "thread_id": "29",
        "content_message_ids": ["41", "42"],
        "control_message_id": "44",
    }
    (pending_dir / "abcd1234.json").write_text(json.dumps(record), encoding="utf-8")
    bot = FakeBot()
    applied = []
    service = plugin.TelegramSkillReviewService(
        bot=bot,
        adapter=FakeAdapter(),
        store=plugin.ReviewStore(tmp_path),
        chat_id="17",
        apply_pending=lambda payload: applied.append(payload) or {"success": True},
    )
    query = FakeQuery("skillreview:a:default:abcd1234")

    asyncio.run(service.handle_callback(query))

    assert applied == [record["payload"]]
    assert not (pending_dir / "abcd1234.json").exists()
    assert any(call[0] == "edit_message_reply_markup" for call in bot.calls)
    assert not any(call[0] == "delete_message" for call in bot.calls)
    ledger = [json.loads(line) for line in (tmp_path / "plugin-data" / "telegram-skill-review" / "review-ledger.jsonl").read_text().splitlines()]
    assert ledger[-1]["decision"] == "approved"
    assert ledger[-1]["content_message_ids"] == ["41", "42"]


def test_approve_is_finalized_before_telegram_status_failure(tmp_path):
    class StatusFailureBot(FakeBot):
        async def edit_message_reply_markup(self, **kwargs):
            raise RuntimeError("Telegram unavailable")

    pending_dir = tmp_path / "pending" / "skills"
    pending_dir.mkdir(parents=True)
    record = pending_record()
    record["review"] = {
        "delivery_status": "sent",
        "chat_id": "17",
        "thread_id": "29",
        "content_message_ids": ["41"],
        "control_message_id": "44",
    }
    (pending_dir / "abcd1234.json").write_text(json.dumps(record), encoding="utf-8")
    applied = []
    service = plugin.TelegramSkillReviewService(
        bot=StatusFailureBot(),
        adapter=FakeAdapter(),
        store=plugin.ReviewStore(tmp_path),
        chat_id="17",
        apply_pending=lambda payload: applied.append(payload) or {"success": True},
    )

    try:
        asyncio.run(service.handle_callback(FakeQuery("skillreview:a:default:abcd1234")))
    except RuntimeError:
        pass

    assert len(applied) == 1
    assert not (pending_dir / "abcd1234.json").exists()


def test_callback_uses_the_actual_telegram_chat_type(tmp_path):
    pending_dir = tmp_path / "pending" / "skills"
    pending_dir.mkdir(parents=True)
    record = pending_record()
    record["review"] = {
        "delivery_status": "sent",
        "chat_id": "17",
        "thread_id": "29",
        "content_message_ids": ["41"],
        "control_message_id": "44",
    }
    (pending_dir / "abcd1234.json").write_text(json.dumps(record), encoding="utf-8")
    adapter = FakeAdapter()
    service = plugin.TelegramSkillReviewService(
        bot=FakeBot(), adapter=adapter, store=plugin.ReviewStore(tmp_path), chat_id="17"
    )
    query = FakeQuery("skillreview:r:default:abcd1234")
    query.message.chat.type = "supergroup"

    asyncio.run(service.handle_callback(query))

    assert adapter.chat_types == ["supergroup"]


def test_reject_callback_removes_pending_and_reports_status(tmp_path):
    pending_dir = tmp_path / "pending" / "skills"
    pending_dir.mkdir(parents=True)
    record = pending_record()
    record["review"] = {
        "delivery_status": "sent",
        "chat_id": "17",
        "thread_id": "29",
        "content_message_ids": ["41"],
        "control_message_id": "44",
    }
    (pending_dir / "abcd1234.json").write_text(json.dumps(record), encoding="utf-8")
    bot = FakeBot()
    service = plugin.TelegramSkillReviewService(
        bot=bot,
        adapter=FakeAdapter(),
        store=plugin.ReviewStore(tmp_path),
        chat_id="17",
    )
    query = FakeQuery("skillreview:r:default:abcd1234")

    asyncio.run(service.handle_callback(query))

    assert not (pending_dir / "abcd1234.json").exists()
    assert query.answers[0] == "Rejecting proposal…"
    status_calls = [call for call in bot.calls if call[0] == "sendRichMessage"]
    assert "Rejected" in status_calls[-1][1]["rich_message"]["markdown"]


def test_callback_refuses_a_different_source_profile(tmp_path):
    service = plugin.TelegramSkillReviewService(
        bot=FakeBot(),
        adapter=FakeAdapter(),
        store=plugin.ReviewStore(tmp_path),
        chat_id="17",
        profile_name="finago-developer",
    )
    query = FakeQuery("skillreview:a:dgme:abcd1234")

    asyncio.run(service.handle_callback(query))

    assert query.answers == []


def test_source_profile_names_default_to_plugin_profile():
    class Context:
        profile_name = "dgme"

        @staticmethod
        def get_config(key, default=None):
            return default

    assert plugin.source_profile_names(Context()) == ["dgme"]


def test_source_profile_names_accept_multiple_profiles():
    class Context:
        profile_name = "dgme"

        @staticmethod
        def get_config(key, default=None):
            return ["dgme", "spicyme", "dgme"] if key == "source_profiles" else default

    assert plugin.source_profile_names(Context()) == ["dgme", "spicyme"]


def test_source_profile_home_resolves_from_default_home(tmp_path):
    default_home = tmp_path / ".hermes"

    assert plugin.source_profile_home(default_home, "default", "finago-developer") == (
        default_home / "profiles" / "finago-developer"
    )


def test_source_profile_home_resolves_named_profile_sibling(tmp_path):
    dgme_home = tmp_path / ".hermes" / "profiles" / "dgme"

    assert plugin.source_profile_home(dgme_home, "dgme", "spicyme") == dgme_home.parent / "spicyme"


def test_review_ledger_is_private(tmp_path):
    store = plugin.ReviewStore(tmp_path)
    record = pending_record()
    record["review"] = {"content_message_ids": []}
    store.finalize(record, decision="rejected", telegram_user_id="88", status_message_id="")

    assert store.data_dir.stat().st_mode & 0o777 == 0o700
    assert store.ledger_path.stat().st_mode & 0o777 == 0o600
