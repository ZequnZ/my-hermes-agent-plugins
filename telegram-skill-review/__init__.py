from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


logger = logging.getLogger("telegram-skill-review")
CALLBACK_PREFIX = "skillreview"
DEFAULT_MAX_MESSAGE_CHARS = 30_000
DEFAULT_SCAN_INTERVAL_SECONDS = 2.0
DELIVERY_CLAIM_TTL_SECONDS = 120.0


@dataclass(frozen=True)
class ProposalMessage:
    source: str
    markdown: str


def split_markdown(content: str, max_chars: int = DEFAULT_MAX_MESSAGE_CHARS) -> list[str]:
    if max_chars < 1:
        raise ValueError("max_chars must be positive")
    if not content:
        return [""]

    parts: list[str] = []
    remaining = content
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        split_at = max(window.rfind("\n\n"), window.rfind("\n"))
        if split_at < max_chars // 2:
            split_at = max_chars
        else:
            split_at += 2 if remaining[split_at:split_at + 2] == "\n\n" else 1
        parts.append(remaining[:split_at])
        remaining = remaining[split_at:]
    if remaining or not parts:
        parts.append(remaining)
    return parts


def render_proposal_messages(record: dict[str, Any], max_chars: int = DEFAULT_MAX_MESSAGE_CHARS) -> list[ProposalMessage]:
    payload = record.get("payload", {})
    content = str(payload.get("content") or "")
    if payload.get("action") == "batch":
        content = _render_batch_payload(payload)
    pending_id = str(record.get("id") or "")
    name = str(record.get("payload", {}).get("name") or "unnamed-skill")
    profile = str(record.get("profile") or "unknown")
    prefix_template = (
        "# Skill proposal: `{name}`\n\n"
        "**Profile:** `{profile}`  \n"
        "**Pending ID:** `{pending_id}`  \n"
        "**Part {part}/{total}**\n\n"
    )

    minimum_prefix = len(
        prefix_template.format(name=name, profile=profile, pending_id=pending_id, part=1, total=1)
    )
    if max_chars <= minimum_prefix:
        raise ValueError(f"max_message_chars must be greater than the {minimum_prefix}-character proposal header")

    total = 1
    while True:
        prefix_budget = len(
            prefix_template.format(
                name=name,
                profile=profile,
                pending_id=pending_id,
                part=total,
                total=total,
            )
        )
        chunks = split_markdown(content, max_chars=max(1, max_chars - prefix_budget))
        if len(chunks) == total:
            break
        total = len(chunks)

    return [
        ProposalMessage(
            source=chunk,
            markdown=prefix_template.format(
                name=name,
                profile=profile,
                pending_id=pending_id,
                part=index,
                total=total,
            ) + chunk,
        )
        for index, chunk in enumerate(chunks, start=1)
    ]


def _render_batch_payload(payload: dict[str, Any]) -> str:
    parts: list[str] = []
    operations = [operation for operation in payload.get("operations", []) if isinstance(operation, dict)]
    for index, operation in enumerate(operations, start=1):
        action = str(operation.get("action") or "unknown")
        name = str(operation.get("name") or "unnamed-skill")
        parts.append(f"## Operation {index}/{len(operations)}: {action} `{name}`\n\n")
        body = operation.get("content") or operation.get("file_content")
        if body is not None:
            parts.append(str(body))
        elif action == "patch":
            parts.append(
                "```diff\n"
                f"- {operation.get('old_string') or ''}\n"
                f"+ {operation.get('new_string') or ''}\n"
                "```"
            )
        else:
            parts.append(json.dumps(operation, ensure_ascii=False, indent=2))
        parts.append("\n\n")
    return "".join(parts)


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.new")
    temporary.write_text(encoded, encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(path)


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    import fcntl

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ReviewStore:
    def __init__(self, home: Path):
        self.home = Path(home)
        self.pending_dir = self.home / "pending" / "skills"
        self.data_dir = self.home / "plugin-data" / "telegram-skill-review"
        self.lock_dir = self.data_dir / "locks"
        self.ledger_path = self.data_dir / "review-ledger.jsonl"

    def _pending_path(self, pending_id: str) -> Path:
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", pending_id):
            raise ValueError("invalid pending id")
        return self.pending_dir / f"{pending_id}.json"

    def _lock_path(self, pending_id: str) -> Path:
        return self.lock_dir / f"{pending_id}.lock"

    def list_pending_ids(self) -> list[str]:
        if not self.pending_dir.exists():
            return []
        return sorted(
            path.stem
            for path in self.pending_dir.glob("*.json")
            if re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", path.stem)
        )

    def get_pending(self, pending_id: str) -> dict[str, Any] | None:
        path = self._pending_path(pending_id)
        if not path.exists():
            return None
        value = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(value, dict):
            raise TypeError(f"pending record {pending_id} must be an object")
        return value

    def _update(self, pending_id: str, mutate: Callable[[dict[str, Any]], None]) -> dict[str, Any] | None:
        with _file_lock(self._lock_path(pending_id)):
            record = self.get_pending(pending_id)
            if record is None:
                return None
            mutate(record)
            _atomic_write_json(self._pending_path(pending_id), record)
            return record

    def claim_for_delivery(self, pending_id: str) -> dict[str, Any] | None:
        now = time.time()

        def claim(record: dict[str, Any]) -> None:
            review = record.setdefault("review", {})
            status = review.get("delivery_status")
            claimed_at = float(review.get("claimed_at") or 0)
            next_attempt_at = float(review.get("next_attempt_at") or 0)
            if (
                status == "sent"
                or next_attempt_at > now
                or (status == "delivering" and now - claimed_at < DELIVERY_CLAIM_TTL_SECONDS)
            ):
                raise AlreadyClaimed
            review.update(delivery_status="delivering", claimed_at=now)

        try:
            return self._update(pending_id, claim)
        except AlreadyClaimed:
            return None

    def record_topic(self, pending_id: str, *, chat_id: str, thread_id: str) -> None:
        self._update(
            pending_id,
            lambda record: record.setdefault("review", {}).update(
                chat_id=str(chat_id),
                thread_id=str(thread_id),
                content_message_ids=record.setdefault("review", {}).get("content_message_ids", []),
            ),
        )

    def record_content_message(self, pending_id: str, *, message_id: str) -> None:
        def mutate(record: dict[str, Any]) -> None:
            messages = record.setdefault("review", {}).setdefault("content_message_ids", [])
            value = str(message_id)
            if value not in messages:
                messages.append(value)

        self._update(pending_id, mutate)

    def record_control_message(self, pending_id: str, *, message_id: str) -> None:
        self._update(
            pending_id,
            lambda record: record.setdefault("review", {}).update(
                control_message_id=str(message_id),
                delivery_status="sent",
                sent_at=time.time(),
            ),
        )

    def record_delivery_failure(self, pending_id: str, error: str) -> None:
        failed_at = time.time()

        def mutate(record: dict[str, Any]) -> None:
            review = record.setdefault("review", {})
            attempts = int(review.get("attempts") or 0) + 1
            delay = min(3600.0, 5.0 * (2 ** min(attempts - 1, 9)))
            review.update(
                delivery_status="retry",
                attempts=attempts,
                failed_at=failed_at,
                next_attempt_at=failed_at + delay,
                last_error=str(error)[:1000],
            )

        self._update(pending_id, mutate)

    def claim_decision(self, pending_id: str, decision: str) -> dict[str, Any] | None:
        def mutate(record: dict[str, Any]) -> None:
            review = record.setdefault("review", {})
            if review.get("decision_status") == "processing":
                raise AlreadyClaimed
            review.update(decision_status="processing", requested_decision=decision, decision_claimed_at=time.time())

        try:
            return self._update(pending_id, mutate)
        except AlreadyClaimed:
            return None

    def release_decision(self, pending_id: str) -> None:
        def mutate(record: dict[str, Any]) -> None:
            review = record.setdefault("review", {})
            review.pop("decision_status", None)
            review.pop("requested_decision", None)
            review.pop("decision_claimed_at", None)

        self._update(pending_id, mutate)

    def finalize(self, record: dict[str, Any], *, decision: str, telegram_user_id: str, status_message_id: str) -> None:
        review = record.get("review") or {}
        entry = {
            "pending_id": record.get("id"),
            "skill_name": record.get("payload", {}).get("name"),
            "decision": decision,
            "telegram_user_id": telegram_user_id,
            "chat_id": review.get("chat_id"),
            "thread_id": review.get("thread_id"),
            "content_message_ids": list(review.get("content_message_ids") or []),
            "control_message_id": review.get("control_message_id"),
            "status_message_id": status_message_id,
            "decided_at": datetime.now(UTC).isoformat(),
        }
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.data_dir, 0o700)
        with _file_lock(self.data_dir / "review-ledger.lock"):
            with self.ledger_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(self.ledger_path, 0o600)
        self._pending_path(str(record["id"])).unlink(missing_ok=True)


class AlreadyClaimed(Exception):
    pass


class TelegramSkillReviewService:
    def __init__(
        self,
        *,
        bot: Any,
        adapter: Any,
        store: ReviewStore,
        chat_id: str,
        max_message_chars: int = DEFAULT_MAX_MESSAGE_CHARS,
        scan_interval_seconds: float = DEFAULT_SCAN_INTERVAL_SECONDS,
        apply_pending: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        include_foreground: bool = False,
        profile_name: str = "default",
    ):
        self.bot = bot
        self.adapter = adapter
        self.store = store
        self.chat_id = str(chat_id)
        self.max_message_chars = int(max_message_chars)
        self.scan_interval_seconds = float(scan_interval_seconds)
        self.apply_pending = apply_pending or self._apply_pending
        self.include_foreground = bool(include_foreground)
        self.profile_name = str(profile_name or "default")

    @staticmethod
    def _apply_pending(payload: dict[str, Any]) -> dict[str, Any]:
        from tools.skill_manager_tool import apply_skill_pending

        return json.loads(apply_skill_pending(payload))

    @staticmethod
    def _message_id(response: Any) -> str:
        if isinstance(response, dict):
            value = response.get("message_id")
            if value is None and isinstance(response.get("result"), dict):
                value = response["result"].get("message_id")
        else:
            value = getattr(response, "message_id", None)
        if value is None:
            raise RuntimeError("Telegram response did not include message_id")
        return str(value)

    async def _send_rich(self, markdown: str, *, thread_id: str, reply_markup: dict[str, Any] | None = None) -> str:
        payload: dict[str, Any] = {
            "chat_id": int(self.chat_id),
            "message_thread_id": int(thread_id),
            "rich_message": {"markdown": markdown},
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        response = await self.bot.do_api_request("sendRichMessage", api_kwargs=payload)
        return self._message_id(response)

    def _eligible(self, record: dict[str, Any]) -> bool:
        return record.get("subsystem") == "skills" and (
            self.include_foreground or record.get("origin") == "background_review"
        )

    async def deliver_pending(self, pending_id: str) -> None:
        candidate = self.store.get_pending(pending_id)
        if candidate is None or not self._eligible(candidate):
            return
        record = self.store.claim_for_delivery(pending_id)
        if record is None:
            return
        try:
            if record.get("profile") != self.profile_name:
                record = self.store._update(
                    pending_id,
                    lambda current: current.update(profile=self.profile_name),
                ) or record
            review = record.get("review") or {}
            existing_thread_id = review.get("thread_id")
            thread_id = existing_thread_id
            name = str(record.get("payload", {}).get("name") or "unnamed-skill")
            if not thread_id:
                topic = await self.bot.create_forum_topic(
                    chat_id=int(self.chat_id),
                    name=f"Skill · {self.profile_name} · {name} · {pending_id}"[:128],
                )
                thread_id = str(topic.message_thread_id)
                self.store.record_topic(pending_id, chat_id=self.chat_id, thread_id=thread_id)

            delivered = len(review.get("content_message_ids") or []) if existing_thread_id else 0
            messages = render_proposal_messages(record, self.max_message_chars)
            for message in messages[delivered:]:
                message_id = await self._send_rich(message.markdown, thread_id=thread_id)
                self.store.record_content_message(pending_id, message_id=message_id)

            control_markdown = (
                "## Review\n\n"
                "The complete proposed `SKILL.md` is preserved in the messages above. "
                "Approval applies the stored pending payload directly."
            )
            keyboard = {
                "inline_keyboard": [[
                    {
                        "text": "Approve",
                        "callback_data": f"{CALLBACK_PREFIX}:a:{self.profile_name}:{pending_id}",
                    },
                    {
                        "text": "Reject",
                        "callback_data": f"{CALLBACK_PREFIX}:r:{self.profile_name}:{pending_id}",
                    },
                ]]
            }
            control_id = await self._send_rich(control_markdown, thread_id=thread_id, reply_markup=keyboard)
            self.store.record_control_message(pending_id, message_id=control_id)
        except Exception as exc:
            self.store.record_delivery_failure(pending_id, str(exc))
            logger.exception("Failed to deliver pending skill review %s", pending_id)

    async def scan_once(self) -> None:
        for pending_id in self.store.list_pending_ids():
            await self.deliver_pending(pending_id)

    async def run_scanner(self) -> None:
        while True:
            await self.scan_once()
            await asyncio.sleep(self.scan_interval_seconds)

    async def handle_callback(self, query: Any) -> None:
        match = re.fullmatch(
            rf"{CALLBACK_PREFIX}:([ar]):([a-zA-Z0-9_-]{{1,64}}):([a-zA-Z0-9_-]{{1,64}})",
            str(query.data or ""),
        )
        if match is None:
            return
        choice, callback_profile, pending_id = match.groups()
        if callback_profile != self.profile_name:
            return
        message = getattr(query, "message", None)
        chat_id = getattr(message, "chat_id", None)
        thread_id = getattr(message, "message_thread_id", None)
        chat = getattr(message, "chat", None)
        chat_type = str(getattr(chat, "type", "private"))
        authorized = self.adapter._is_callback_user_authorized(
            str(getattr(query.from_user, "id", "")),
            chat_id=str(chat_id) if chat_id is not None else None,
            chat_type=chat_type,
            thread_id=str(thread_id) if thread_id is not None else None,
            user_name=getattr(query.from_user, "first_name", None),
        )
        if not authorized:
            await query.answer(text="Unauthorized")
            return

        await query.answer(text="Applying skill…" if choice == "a" else "Rejecting proposal…")
        record = self.store.claim_decision(pending_id, "approved" if choice == "a" else "rejected")
        if record is None:
            await query.answer(text="This proposal has already been reviewed.")
            return
        review = record.get("review") or {}
        if str(review.get("chat_id")) != str(chat_id) or str(review.get("thread_id")) != str(thread_id):
            self.store.release_decision(pending_id)
            await query.answer(text="This review button does not match the proposal topic.")
            return

        if choice == "a":
            result = self.apply_pending(dict(record.get("payload") or {}))
            if not result.get("success"):
                self.store.release_decision(pending_id)
                error = str(result.get("error") or "Skill validation failed")
                await self._send_rich(
                    f"## Approval failed ⚠️\n\nThe proposal remains pending.\n\n**Error:** {error}",
                    thread_id=str(thread_id),
                )
                return
            decision = "approved"
            status = f"## Approved ✅\n\n`{record.get('payload', {}).get('name', 'skill')}` was saved successfully."
            topic_name = f"✅ {record.get('payload', {}).get('name', 'skill')}"
        else:
            decision = "rejected"
            status = (
                "## Rejected ❌\n\nNo skill files were saved. "
                "The complete proposed `SKILL.md` remains in this topic for future reference."
            )
            topic_name = f"❌ {record.get('payload', {}).get('name', 'skill')}"

        self.store.finalize(
            record,
            decision=decision,
            telegram_user_id=str(getattr(query.from_user, "id", "")),
            status_message_id="",
        )
        await self.bot.edit_message_reply_markup(
            chat_id=int(str(chat_id)),
            message_id=int(review.get("control_message_id") or query.message.message_id),
            reply_markup=None,
        )
        await self._send_rich(status, thread_id=str(thread_id))
        await self.bot.edit_forum_topic(
            chat_id=int(str(chat_id)),
            message_thread_id=int(str(thread_id)),
            name=topic_name[:128],
        )


def _resolve_chat_id(ctx: Any) -> str:
    configured = str(ctx.get_config("chat_id", default="") or "").strip()
    return configured or str(os.getenv("TELEGRAM_HOME_CHANNEL", "")).strip()


def source_profile_names(ctx: Any) -> list[str]:
    configured = ctx.get_config("source_profiles", default=[])
    values = configured if isinstance(configured, list) and configured else [ctx.profile_name]
    names: list[str] = []
    for value in values:
        name = str(value or "").strip()
        if re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name) and name not in names:
            names.append(name)
    return names or [ctx.profile_name]


def source_profile_home(plugin_home: Path, plugin_profile: str, source_profile: str) -> Path:
    if source_profile == plugin_profile:
        return plugin_home
    if plugin_profile == "default":
        return plugin_home / "profiles" / source_profile
    if source_profile == "default":
        return plugin_home.parent.parent
    return plugin_home.parent / source_profile


def register(ctx: Any) -> None:
    from hermes_constants import get_hermes_home

    home = get_hermes_home()
    chat_id = _resolve_chat_id(ctx)
    source_profiles = source_profile_names(ctx)
    if not chat_id:
        logger.warning("telegram-skill-review is enabled without chat_id or TELEGRAM_HOME_CHANNEL")

    def wire(application: Any, adapter: Any) -> None:
        from telegram.ext import CallbackQueryHandler

        if not chat_id:
            return
        services: dict[str, TelegramSkillReviewService] = {}
        for profile_name in source_profiles:
            source_home = source_profile_home(home, ctx.profile_name, profile_name)
            services[profile_name] = TelegramSkillReviewService(
                bot=application.bot,
                adapter=adapter,
                store=ReviewStore(source_home),
                chat_id=chat_id,
                max_message_chars=int(ctx.get_config("max_message_chars", DEFAULT_MAX_MESSAGE_CHARS)),
                scan_interval_seconds=float(ctx.get_config("scan_interval_seconds", DEFAULT_SCAN_INTERVAL_SECONDS)),
                include_foreground=bool(ctx.get_config("include_foreground", False)),
                profile_name=profile_name,
            )

        async def callback(update: Any, context: Any) -> None:
            query = update.callback_query
            if query is None:
                return
            match = re.fullmatch(
                rf"{CALLBACK_PREFIX}:[ar]:([a-zA-Z0-9_-]{{1,64}}):[a-zA-Z0-9_-]{{1,64}}",
                str(query.data or ""),
            )
            if match and match.group(1) in services:
                await services[match.group(1)].handle_callback(query)

        application.add_handler(
            CallbackQueryHandler(
                callback,
                pattern=(
                    rf"^{CALLBACK_PREFIX}:[ar]:(?:{'|'.join(map(re.escape, source_profiles))}):"
                    r"[a-zA-Z0-9_-]{1,64}$"
                ),
            )
        )
        for profile_name, service in services.items():
            ctx.spawn_task(
                service.run_scanner(),
                name=f"telegram-skill-review-scanner:{profile_name}",
            )

    ctx.register_platform_handler("telegram", wire)
