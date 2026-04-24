# -*- coding: utf-8 -*-
# ------------------------------------------------------------------------------
#
#   Copyright 2026 Valory AG
#
#   Licensed under the Apache License, Version 2.0 (the "License");
#   you may not use this file except in compliance with the License.
#   You may obtain a copy of the License at
#
#       http://www.apache.org/licenses/LICENSE-2.0
#
#   Unless required by applicable law or agreed to in writing, software
#   distributed under the License is distributed on an "AS IS" BASIS,
#   WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#   See the License for the specific language governing permissions and
#   limitations under the License.
#
# ------------------------------------------------------------------------------

"""Tests for the HttpHandler in the composed skill."""

import json
from datetime import datetime
from typing import Any, Optional
from unittest.mock import MagicMock, patch

from packages.valory.connections.http_server.connection import (
    PUBLIC_ID as HTTP_SERVER_PUBLIC_ID,
)
from packages.valory.protocols.http import HttpMessage
from packages.valory.skills.market_resolver_abci.handlers import (
    HttpCode,
    HttpHandler,
    HttpMethod,
)


def _make_context(service_endpoint_base: str = "https://example.com/") -> MagicMock:
    """Build a mocked skill context for HttpHandler tests."""
    context = MagicMock()
    context.params.service_endpoint_base = service_endpoint_base
    context.params.reset_pause_duration = 30
    context.logger = MagicMock()
    context.outbox = MagicMock()
    context.http_dialogues = MagicMock()
    context.state = MagicMock()
    return context


def _make_handler(context: Any = None) -> HttpHandler:
    """Instantiate and set up HttpHandler."""
    if context is None:
        context = _make_context()
    handler = HttpHandler(name="http", skill_context=context)
    handler.setup()
    return handler


def _make_http_message(
    url: str = "http://localhost:8000/healthcheck",
    method: str = "get",
    body: bytes = b"",
    performative: Any = None,
    sender: Optional[str] = None,
) -> MagicMock:
    """Build a mocked HttpMessage."""
    msg = MagicMock(spec=HttpMessage)
    msg.url = url
    msg.method = method
    msg.body = body
    msg.performative = performative or HttpMessage.Performative.REQUEST
    msg.sender = sender or str(HTTP_SERVER_PUBLIC_ID.without_hash())
    msg.version = "1.1"
    msg.headers = ""
    return msg


class TestHttpHandlerSetup:
    """Tests for HttpHandler.setup()."""

    def test_setup_creates_routes(self) -> None:
        """setup() populates self.routes."""
        handler = _make_handler()
        assert hasattr(handler, "routes")
        assert len(handler.routes) > 0

    def test_setup_creates_handler_url_regex(self) -> None:
        """setup() sets handler_url_regex."""
        handler = _make_handler()
        assert hasattr(handler, "handler_url_regex")
        assert "localhost" in handler.handler_url_regex

    def test_setup_propel_hostname_in_regex(self) -> None:
        """Propel hostname pattern appears in the regex."""
        handler = _make_handler()
        assert "propel" in handler.handler_url_regex


class TestGetHandler:
    """Tests for HttpHandler._get_handler."""

    def test_localhost_healthcheck_get_matched(self) -> None:
        """GET /healthcheck on localhost is matched to _handle_get_health."""
        handler = _make_handler()
        func, kwargs = handler._get_handler("http://localhost:8000/healthcheck", "get")
        assert func is not None
        assert func == handler._handle_get_health

    def test_localhost_healthcheck_head_matched(self) -> None:
        """HEAD /healthcheck on localhost is matched."""
        handler = _make_handler()
        func, kwargs = handler._get_handler("http://localhost:8000/healthcheck", "head")
        assert func is not None

    def test_unknown_path_returns_bad_request(self) -> None:
        """Unknown path on known host returns _handle_bad_request."""
        handler = _make_handler()
        func, kwargs = handler._get_handler("http://localhost:8000/unknown", "get")
        assert func == handler._handle_bad_request

    def test_unknown_host_returns_none(self) -> None:
        """URL not matching any known host returns None."""
        handler = _make_handler()
        # "unknown-host.test" contains none of the whitelisted tokens
        # (example.com, localhost, 127.0.0.1, 0.0.0.0, or a Propel subdomain).
        func, kwargs = handler._get_handler(
            "http://unknown-host.test/healthcheck", "get"
        )
        assert func is None

    def test_post_to_healthcheck_returns_bad_request(self) -> None:
        """POST to /healthcheck falls through to bad request (no POST routes defined)."""
        handler = _make_handler()
        func, kwargs = handler._get_handler("http://localhost:8000/healthcheck", "post")
        assert func == handler._handle_bad_request


class TestHandleMethod:
    """Tests for HttpHandler.handle dispatch."""

    def test_non_request_performative_calls_super(self) -> None:
        """Non-REQUEST performative delegates to super().handle."""
        handler = _make_handler()
        msg = _make_http_message()
        msg.performative = HttpMessage.Performative.RESPONSE  # not REQUEST

        with patch.object(type(handler).__bases__[0], "handle") as mock_super:
            handler.handle(msg)
            mock_super.assert_called_once()

    def test_non_http_server_sender_calls_super(self) -> None:
        """Message from unknown sender delegates to super().handle."""
        handler = _make_handler()
        msg = _make_http_message(sender="not:the:http_server")

        with patch.object(type(handler).__bases__[0], "handle") as mock_super:
            handler.handle(msg)
            mock_super.assert_called_once()

    def test_url_not_matching_calls_super(self) -> None:
        """URL not matching handler regex calls super().handle."""
        handler = _make_handler()
        msg = _make_http_message(url="http://unknown-host.test/healthcheck")

        with patch.object(type(handler).__bases__[0], "handle") as mock_super:
            handler.handle(msg)
            mock_super.assert_called_once()

    def test_valid_request_dispatches_to_handler(self) -> None:
        """Valid request dispatches to the matched route handler."""
        msg = _make_http_message(url="http://localhost:8000/healthcheck", method="get")

        # Patch the CLASS method before handler.setup() runs, so the
        # route table stores a reference to the patched callable.
        with patch.object(HttpHandler, "_handle_get_health") as mock_health:
            handler = _make_handler()
            mock_dialogue = MagicMock()
            handler.context.http_dialogues.update.return_value = mock_dialogue
            handler.handle(msg)
            mock_health.assert_called_once()

    def test_invalid_dialogue_returns_early(self) -> None:
        """None dialogue returns without calling handler."""
        msg = _make_http_message(url="http://localhost:8000/healthcheck", method="get")

        with patch.object(HttpHandler, "_handle_get_health") as mock_health:
            handler = _make_handler()
            handler.context.http_dialogues.update.return_value = None
            handler.handle(msg)
            mock_health.assert_not_called()


class TestHandleBadRequest:
    """Tests for HttpHandler._handle_bad_request."""

    def test_puts_bad_request_response(self) -> None:
        """_handle_bad_request puts a 400 response in the outbox."""
        handler = _make_handler()
        msg = _make_http_message()
        dialogue = MagicMock()
        mock_response = MagicMock()
        dialogue.reply.return_value = mock_response

        handler._handle_bad_request(msg, dialogue)

        dialogue.reply.assert_called_once()
        call_kwargs = dialogue.reply.call_args[1]
        assert call_kwargs["status_code"] == HttpCode.BAD_REQUEST_CODE.value
        handler.context.outbox.put_message.assert_called_once_with(
            message=mock_response
        )


class TestSendOkResponse:
    """Tests for HttpHandler._send_ok_response."""

    def test_sends_ok_with_json_body(self) -> None:
        """_send_ok_response sends a 200 with JSON-encoded body."""
        handler = _make_handler()
        msg = _make_http_message()
        dialogue = MagicMock()
        mock_response = MagicMock()
        dialogue.reply.return_value = mock_response

        data = {"key": "value"}
        handler._send_ok_response(msg, dialogue, data)

        dialogue.reply.assert_called_once()
        call_kwargs = dialogue.reply.call_args[1]
        assert call_kwargs["status_code"] == HttpCode.OK_CODE.value
        assert json.loads(call_kwargs["body"]) == data
        handler.context.outbox.put_message.assert_called_once()


class TestHandleGetHealth:
    """Tests for HttpHandler._handle_get_health."""

    def _setup_handler_with_round_sequence(
        self,
        last_round_ts: Any = None,
        block_stall: bool = False,
        has_abci_app: bool = False,
    ) -> HttpHandler:
        """Set up handler with controlled round_sequence mock."""
        context = _make_context()
        context.params.reset_pause_duration = 30
        handler = HttpHandler(name="http", skill_context=context)
        handler.setup()

        round_sequence = MagicMock()
        round_sequence._last_round_transition_timestamp = last_round_ts
        round_sequence.block_stall_deadline_expired = block_stall
        round_sequence.latest_synchronized_data = MagicMock()
        round_sequence.latest_synchronized_data.db = MagicMock()
        # SynchronizedData.period_count reads db.reset_index -- force an int
        # so the health payload is JSON-serializable.
        round_sequence.latest_synchronized_data.db.reset_index = 0

        if has_abci_app:
            round_sequence._abci_app = MagicMock()
            round_sequence._abci_app.current_round.round_id = "scan_markets_round"
            round_sequence._abci_app._previous_rounds = []
        else:
            round_sequence._abci_app = None

        context.state.round_sequence = round_sequence
        return handler

    def test_no_last_round_ts_returns_nulls(self) -> None:
        """When no last_round_transition_timestamp, times are None."""
        handler = self._setup_handler_with_round_sequence(last_round_ts=None)
        msg = _make_http_message()
        dialogue = MagicMock()
        dialogue.reply.return_value = MagicMock()

        handler._handle_get_health(msg, dialogue)

        call_kwargs = dialogue.reply.call_args[1]
        body = json.loads(call_kwargs["body"])
        assert body["seconds_since_last_transition"] is None
        assert (
            body["is_tm_healthy"] is not False
        )  # is_tm_unhealthy=None -> not None = True

    def test_with_last_round_ts_computes_seconds(self) -> None:
        """With a valid timestamp, seconds_since_last_transition is computed."""
        past_ts = datetime(2026, 1, 1, 0, 0, 0)
        handler = self._setup_handler_with_round_sequence(
            last_round_ts=past_ts,
            block_stall=False,
        )
        msg = _make_http_message()
        dialogue = MagicMock()
        dialogue.reply.return_value = MagicMock()

        handler._handle_get_health(msg, dialogue)

        call_kwargs = dialogue.reply.call_args[1]
        body = json.loads(call_kwargs["body"])
        assert body["seconds_since_last_transition"] is not None
        assert body["seconds_since_last_transition"] > 0

    def test_block_stall_reflected_in_is_tm_healthy(self) -> None:
        """block_stall_deadline_expired=True -> is_tm_healthy=False."""
        past_ts = datetime(2026, 1, 1, 0, 0, 0)
        handler = self._setup_handler_with_round_sequence(
            last_round_ts=past_ts, block_stall=True
        )
        msg = _make_http_message()
        dialogue = MagicMock()
        dialogue.reply.return_value = MagicMock()

        handler._handle_get_health(msg, dialogue)

        call_kwargs = dialogue.reply.call_args[1]
        body = json.loads(call_kwargs["body"])
        assert body["is_tm_healthy"] is False

    def test_with_abci_app_populates_rounds(self) -> None:
        """When _abci_app is set, rounds list is populated."""
        past_ts = datetime(2026, 1, 1, 0, 0, 0)
        handler = self._setup_handler_with_round_sequence(
            last_round_ts=past_ts,
            block_stall=False,
            has_abci_app=True,
        )
        msg = _make_http_message()
        dialogue = MagicMock()
        dialogue.reply.return_value = MagicMock()

        handler._handle_get_health(msg, dialogue)

        call_kwargs = dialogue.reply.call_args[1]
        body = json.loads(call_kwargs["body"])
        assert body["rounds"] is not None
        assert "scan_markets_round" in body["rounds"]

    def test_no_abci_app_rounds_is_none(self) -> None:
        """When _abci_app is None, rounds is None."""
        handler = self._setup_handler_with_round_sequence(
            last_round_ts=None, has_abci_app=False
        )
        msg = _make_http_message()
        dialogue = MagicMock()
        dialogue.reply.return_value = MagicMock()

        handler._handle_get_health(msg, dialogue)

        call_kwargs = dialogue.reply.call_args[1]
        body = json.loads(call_kwargs["body"])
        assert body["rounds"] is None


class TestHttpCodeAndHttpMethod:
    """Smoke tests for the HttpCode and HttpMethod enums."""

    def test_http_code_ok(self) -> None:
        """OK_CODE is 200."""
        assert HttpCode.OK_CODE.value == 200

    def test_http_code_not_found(self) -> None:
        """NOT_FOUND_CODE is 404."""
        assert HttpCode.NOT_FOUND_CODE.value == 404

    def test_http_code_bad_request(self) -> None:
        """BAD_REQUEST_CODE is 400."""
        assert HttpCode.BAD_REQUEST_CODE.value == 400

    def test_http_code_not_ready(self) -> None:
        """NOT_READY is 503."""
        assert HttpCode.NOT_READY.value == 503

    def test_http_method_values(self) -> None:
        """Enum HttpMethod exposes GET, HEAD, POST members."""
        assert HttpMethod.GET.value == "get"
        assert HttpMethod.HEAD.value == "head"
        assert HttpMethod.POST.value == "post"
