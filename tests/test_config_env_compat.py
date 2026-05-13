# -*- coding: utf-8 -*-
"""Tests for backward-compatible config env aliases and TickFlow loading."""

import os
import socket
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.config import Config, setup_env


class _FakeHeaders:
    def __init__(self, charset: str = "utf-8"):
        self._charset = charset

    def get_content_charset(self) -> str:
        return self._charset


class _FakeUrlopenResponse:
    def __init__(self, payload: str, charset: str = "utf-8", final_url=None):
        self._payload = payload.encode("utf-8")
        self._final_url = final_url
        self.read_called = False
        self.headers = _FakeHeaders(charset)

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _size: int = -1) -> bytes:
        self.read_called = True
        return self._payload

    def geturl(self):
        return self._final_url


class ConfigEnvCompatibilityTestCase(unittest.TestCase):
    def setUp(self):
        self._getaddrinfo_patcher = patch(
            "src.config.socket.getaddrinfo",
            return_value=[
                (
                    socket.AF_INET,
                    socket.SOCK_STREAM,
                    socket.IPPROTO_TCP,
                    "",
                    ("93.184.216.34", 443),
                )
            ],
        )
        self.mock_getaddrinfo = self._getaddrinfo_patcher.start()

    def tearDown(self):
        self._getaddrinfo_patcher.stop()
        Config.reset_instance()

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_load_from_env_reads_tickflow_api_key(
        self, _mock_parse_litellm_yaml, _mock_setup_env
    ):
        with patch.dict(
            os.environ,
            {
                "STOCK_LIST": "600519",
                "TICKFLOW_API_KEY": "tf-secret",
            },
            clear=True,
        ):
            config = Config._load_from_env()

        self.assertEqual(config.tickflow_api_key, "tf-secret")

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_load_from_env_keeps_default_behavior_without_tickflow_api_key(
        self, _mock_parse_litellm_yaml, _mock_setup_env
    ):
        with patch.dict(
            os.environ,
            {
                "STOCK_LIST": "600519",
            },
            clear=True,
        ):
            config = Config._load_from_env()

        self.assertIsNone(config.tickflow_api_key)
        self.assertEqual(
            config.realtime_source_priority,
            "tencent,akshare_sina,efinance,akshare_em",
        )

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_load_from_env_uses_stable_fundamental_timeout_defaults(
        self, _mock_parse_litellm_yaml, _mock_setup_env
    ):
        with patch.dict(
            os.environ,
            {
                "STOCK_LIST": "600519",
            },
            clear=True,
        ):
            config = Config._load_from_env()

        self.assertEqual(config.fundamental_stage_timeout_seconds, 8.0)
        self.assertEqual(config.fundamental_fetch_timeout_seconds, 3.0)

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_schedule_run_immediately_falls_back_to_legacy_run_immediately(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        env = {
            "RUN_IMMEDIATELY": "false",
        }

        with patch.dict(os.environ, env, clear=True):
            config = Config._load_from_env()

        self.assertFalse(config.schedule_run_immediately)
        self.assertFalse(config.run_immediately)

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_schedule_run_immediately_prefers_schedule_specific_setting(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        env = {
            "RUN_IMMEDIATELY": "false",
            "SCHEDULE_RUN_IMMEDIATELY": "true",
        }

        with patch.dict(os.environ, env, clear=True):
            config = Config._load_from_env()

        self.assertTrue(config.schedule_run_immediately)
        self.assertFalse(config.run_immediately)

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_empty_legacy_run_immediately_stays_false_when_schedule_alias_is_unset(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        env = {
            "RUN_IMMEDIATELY": "",
        }

        with patch.dict(os.environ, env, clear=True):
            config = Config._load_from_env()

        self.assertFalse(config.schedule_run_immediately)
        self.assertFalse(config.run_immediately)

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_empty_schedule_run_immediately_stays_false_without_falling_back(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        env = {
            "RUN_IMMEDIATELY": "true",
            "SCHEDULE_RUN_IMMEDIATELY": "",
        }

        with patch.dict(os.environ, env, clear=True):
            config = Config._load_from_env()

        self.assertFalse(config.schedule_run_immediately)
        self.assertTrue(config.run_immediately)

    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_schedule_run_immediately_ignores_persisted_alias_when_only_legacy_env_is_explicit(
        self,
        _mock_parse_yaml,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "STOCK_LIST=600519",
                        "RUN_IMMEDIATELY=true",
                        "SCHEDULE_RUN_IMMEDIATELY=true",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "ENV_FILE": str(env_path),
                    "RUN_IMMEDIATELY": "false",
                },
                clear=True,
            ):
                config = Config._load_from_env()

        self.assertFalse(config.run_immediately)
        self.assertFalse(config.schedule_run_immediately)

    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_blank_schedule_time_falls_back_to_default(
        self,
        _mock_parse_yaml,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "STOCK_LIST=600519",
                        "SCHEDULE_TIME=",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "ENV_FILE": str(env_path),
                },
                clear=True,
            ):
                config = Config._load_from_env()

        self.assertEqual(config.schedule_time, "18:00")

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_report_language_prefers_preexisting_process_env_over_env_file(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text("REPORT_LANGUAGE=zh\n", encoding="utf-8")

            with patch.dict(
                os.environ,
                {
                    "ENV_FILE": str(env_path),
                    "REPORT_LANGUAGE": "en",
                },
                clear=True,
            ):
                config = Config._load_from_env()

        self.assertEqual(config.report_language, "en")

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_report_language_uses_env_file_when_process_env_is_absent(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text("REPORT_LANGUAGE=en\n", encoding="utf-8")

            with patch.dict(
                os.environ,
                {
                    "ENV_FILE": str(env_path),
                },
                clear=True,
            ):
                config = Config._load_from_env()

        self.assertEqual(config.report_language, "en")

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_report_show_llm_model_defaults_true_and_can_be_disabled(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = Config._load_from_env()
        self.assertTrue(config.report_show_llm_model)

        with patch.dict(os.environ, {"REPORT_SHOW_LLM_MODEL": "false"}, clear=True):
            config = Config._load_from_env()
        self.assertFalse(config.report_show_llm_model)

        with patch.dict(os.environ, {"REPORT_SHOW_LLM_MODEL": ""}, clear=True):
            config = Config._load_from_env()
        self.assertFalse(config.report_show_llm_model)

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_market_review_color_scheme_defaults_and_accepts_red_up(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        with patch.dict(os.environ, {}, clear=True):
            config = Config._load_from_env()
        self.assertEqual(config.market_review_color_scheme, "green_up")

        with patch.dict(os.environ, {"MARKET_REVIEW_COLOR_SCHEME": "red-up"}, clear=True):
            config = Config._load_from_env()
        self.assertEqual(config.market_review_color_scheme, "red_up")

    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_runtime_mutable_keys_reload_from_updated_env_file_after_runtime_refresh(
        self,
        _mock_parse_yaml,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "STOCK_LIST=600519",
                        "SCHEDULE_ENABLED=false",
                        "SCHEDULE_TIME=18:00",
                        "RUN_IMMEDIATELY=true",
                        "SCHEDULE_RUN_IMMEDIATELY=false",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "ENV_FILE": str(env_path),
                    "STOCK_LIST": "600519",
                    "SCHEDULE_ENABLED": "false",
                    "SCHEDULE_TIME": "18:00",
                    "RUN_IMMEDIATELY": "true",
                    "SCHEDULE_RUN_IMMEDIATELY": "false",
                },
                clear=True,
            ):
                Config._load_from_env()
                env_path.write_text(
                    "\n".join(
                        [
                            "STOCK_LIST=300750,TSLA",
                            "SCHEDULE_ENABLED=true",
                            "SCHEDULE_TIME=09:30",
                            "RUN_IMMEDIATELY=false",
                            "SCHEDULE_RUN_IMMEDIATELY=true",
                        ]
                    )
                    + "\n",
                    encoding="utf-8",
                )
                Config.reset_instance()
                setup_env(override=True)
                config = Config._load_from_env()

        self.assertEqual(config.stock_list, ["300750", "TSLA"])
        self.assertTrue(config.schedule_enabled)
        self.assertEqual(config.schedule_time, "09:30")
        self.assertFalse(config.run_immediately)
        self.assertTrue(config.schedule_run_immediately)

    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_runtime_mutable_keys_prefer_process_env_when_values_differ(
        self,
        _mock_parse_yaml,
    ) -> None:
        """When process env explicitly sets a WEBUI-mutable key to a value
        that differs from .env (e.g. via docker-compose ``environment:``),
        the process env must win because ``_capture_bootstrap_runtime_env_overrides``
        runs before dotenv loads and the mismatch proves an intentional override.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "STOCK_LIST=300750,TSLA",
                        "SCHEDULE_ENABLED=true",
                        "SCHEDULE_TIME=09:30",
                        "RUN_IMMEDIATELY=false",
                        "SCHEDULE_RUN_IMMEDIATELY=true",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "ENV_FILE": str(env_path),
                    "STOCK_LIST": "600519,000001",
                    "SCHEDULE_ENABLED": "false",
                    "SCHEDULE_TIME": "18:00",
                    "RUN_IMMEDIATELY": "true",
                    "SCHEDULE_RUN_IMMEDIATELY": "false",
                },
                clear=True,
            ):
                config = Config._load_from_env()

        # Explicit process env overrides win when values differ from .env
        self.assertEqual(config.stock_list, ["600519", "000001"])
        self.assertFalse(config.schedule_enabled)
        self.assertEqual(config.schedule_time, "18:00")
        self.assertTrue(config.run_immediately)
        self.assertFalse(config.schedule_run_immediately)

    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_runtime_mutable_keys_use_process_env_when_absent_from_file(
        self,
        _mock_parse_yaml,
    ) -> None:
        """When a WEBUI-mutable key exists only in process env (not in .env),
        it IS a genuine explicit override and must be honoured.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            # .env has no STOCK_LIST or SCHEDULE_* keys at all
            env_path.write_text("LOG_LEVEL=INFO\n", encoding="utf-8")

            with patch.dict(
                os.environ,
                {
                    "ENV_FILE": str(env_path),
                    "STOCK_LIST": "600519,000001",
                },
                clear=True,
            ):
                config = Config._load_from_env()

        self.assertEqual(config.stock_list, ["600519", "000001"])

    @patch("src.config.setup_env")
    @patch("src.config.urllib.request.urlopen")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_stock_list_fetch_api_json_array_overrides_local_stock_list(
        self,
        _mock_parse_yaml,
        mock_urlopen,
        _mock_setup_env,
    ) -> None:
        mock_urlopen.return_value = _FakeUrlopenResponse('["600519", "hk00700", "AAPL"]')

        with patch.dict(
            os.environ,
            {
                "STOCK_LIST": "000001",
                "STOCK_LIST_FETCH_API": "https://example.com/stocks.json",
            },
            clear=True,
        ):
            config = Config._load_from_env()

        self.assertEqual(config.stock_list, ["600519", "HK00700", "AAPL"])
        self.assertEqual(config.stock_list_fetch_api, "https://example.com/stocks.json")

    @patch("src.config.setup_env")
    @patch("src.config.urllib.request.urlopen")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_stock_list_fetch_api_text_payload_supports_commas_and_newlines(
        self,
        _mock_parse_yaml,
        mock_urlopen,
        _mock_setup_env,
    ) -> None:
        mock_urlopen.return_value = _FakeUrlopenResponse("600519,hk00700\nAAPL")

        with patch.dict(
            os.environ,
            {
                "STOCK_LIST": "000001",
                "STOCK_LIST_FETCH_API": "https://example.com/stocks.txt",
            },
            clear=True,
        ):
            config = Config._load_from_env()

        self.assertEqual(config.stock_list, ["600519", "HK00700", "AAPL"])

    @patch("src.config.setup_env")
    @patch("src.config.urllib.request.urlopen")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_stock_list_fetch_api_numeric_text_payload_supports_single_code(
        self,
        _mock_parse_yaml,
        mock_urlopen,
        _mock_setup_env,
    ) -> None:
        mock_urlopen.return_value = _FakeUrlopenResponse("600519")

        with patch.dict(
            os.environ,
            {
                "STOCK_LIST": "000001",
                "STOCK_LIST_FETCH_API": "https://example.com/stocks.txt",
            },
            clear=True,
        ):
            config = Config._load_from_env()

        self.assertEqual(config.stock_list, ["600519"])

    @patch("src.config.setup_env")
    @patch("src.config.urllib.request.urlopen")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_stock_list_fetch_api_accepts_uppercase_scheme(
        self,
        _mock_parse_yaml,
        mock_urlopen,
        _mock_setup_env,
    ) -> None:
        mock_urlopen.return_value = _FakeUrlopenResponse('["600519"]')

        with patch.dict(
            os.environ,
            {
                "STOCK_LIST": "000001",
                "STOCK_LIST_FETCH_API": "HTTPS://example.com/stocks.json",
            },
            clear=True,
        ):
            config = Config._load_from_env()

        self.assertEqual(config.stock_list, ["600519"])

    @patch("src.config.setup_env")
    @patch("src.config.urllib.request.urlopen", side_effect=OSError("offline"))
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_stock_list_fetch_api_failure_falls_back_to_local_stock_list(
        self,
        _mock_parse_yaml,
        _mock_urlopen,
        _mock_setup_env,
    ) -> None:
        with patch.dict(
            os.environ,
            {
                "STOCK_LIST": "000001,300750",
                "STOCK_LIST_FETCH_API": "https://example.com/stocks.json",
            },
            clear=True,
        ):
            config = Config._load_from_env()

        self.assertEqual(config.stock_list, ["000001", "300750"])

    @patch("src.config.setup_env")
    @patch("src.config.urllib.request.urlopen")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_stock_list_fetch_api_malformed_url_falls_back_to_local_stock_list(
        self,
        _mock_parse_yaml,
        mock_urlopen,
        _mock_setup_env,
    ) -> None:
        with patch.dict(
            os.environ,
            {
                "STOCK_LIST": "000001,300750",
                "STOCK_LIST_FETCH_API": "http://[::1",
            },
            clear=True,
        ):
            config = Config._load_from_env()

        mock_urlopen.assert_not_called()
        self.assertEqual(config.stock_list, ["000001", "300750"])

    @patch("src.config.setup_env")
    @patch("src.config.urllib.request.urlopen")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_stock_list_fetch_api_invalid_charset_falls_back_to_local_stock_list(
        self,
        _mock_parse_yaml,
        mock_urlopen,
        _mock_setup_env,
    ) -> None:
        mock_urlopen.return_value = _FakeUrlopenResponse('["600519"]', charset="bad-charset")

        with patch.dict(
            os.environ,
            {
                "STOCK_LIST": "000001,300750",
                "STOCK_LIST_FETCH_API": "https://example.com/stocks.json",
            },
            clear=True,
        ):
            config = Config._load_from_env()

        self.assertEqual(config.stock_list, ["000001", "300750"])

    @patch("src.config.setup_env")
    @patch("src.config.urllib.request.urlopen")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_stock_list_fetch_api_blocks_metadata_endpoint(
        self,
        _mock_parse_yaml,
        mock_urlopen,
        _mock_setup_env,
    ) -> None:
        with patch.dict(
            os.environ,
            {
                "STOCK_LIST": "000001,300750",
                "STOCK_LIST_FETCH_API": "http://169.254.169.254/latest/meta-data",
            },
            clear=True,
        ):
            config = Config._load_from_env()

        mock_urlopen.assert_not_called()
        self.assertEqual(config.stock_list, ["000001", "300750"])

    @patch("src.config.setup_env")
    @patch("src.config.urllib.request.urlopen")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_stock_list_fetch_api_blocks_hostname_resolving_to_metadata_ip(
        self,
        _mock_parse_yaml,
        mock_urlopen,
        _mock_setup_env,
    ) -> None:
        self.mock_getaddrinfo.return_value = [
            (
                socket.AF_INET,
                socket.SOCK_STREAM,
                socket.IPPROTO_TCP,
                "",
                ("169.254.169.254", 80),
            )
        ]

        with patch.dict(
            os.environ,
            {
                "STOCK_LIST": "000001,300750",
                "STOCK_LIST_FETCH_API": "https://watchlist.example/stocks.json",
            },
            clear=True,
        ):
            config = Config._load_from_env()

        mock_urlopen.assert_not_called()
        self.assertEqual(config.stock_list, ["000001", "300750"])

    @patch("src.config.setup_env")
    @patch("src.config.urllib.request.urlopen")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_stock_list_fetch_api_blocks_metadata_redirect_before_reading_response(
        self,
        _mock_parse_yaml,
        mock_urlopen,
        _mock_setup_env,
    ) -> None:
        response = _FakeUrlopenResponse(
            '["600519"]',
            final_url="http://169.254.169.254/latest/meta-data",
        )
        mock_urlopen.return_value = response

        with patch.dict(
            os.environ,
            {
                "STOCK_LIST": "000001,300750",
                "STOCK_LIST_FETCH_API": "https://example.com/stocks.json",
            },
            clear=True,
        ):
            config = Config._load_from_env()

        mock_urlopen.assert_called_once()
        self.assertFalse(response.read_called)
        self.assertEqual(config.stock_list, ["000001", "300750"])

    @patch("src.config.setup_env")
    @patch("src.config.urllib.request.urlopen")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_refresh_stock_list_uses_stock_list_fetch_api(
        self,
        _mock_parse_yaml,
        mock_urlopen,
        _mock_setup_env,
    ) -> None:
        mock_urlopen.return_value = _FakeUrlopenResponse('{"stocks": ["300750", "TSLA"]}')

        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "STOCK_LIST=600519",
                        "STOCK_LIST_FETCH_API=https://example.com/stocks.json",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            config = Config(stock_list=["600519"])

            with patch.dict(os.environ, {"ENV_FILE": str(env_path)}, clear=True):
                config.refresh_stock_list()

        self.assertEqual(config.stock_list, ["300750", "TSLA"])
        self.assertEqual(config.stock_list_fetch_api, "https://example.com/stocks.json")

    @patch("src.config.urllib.request.urlopen")
    def test_refresh_stock_list_preserves_runtime_fetch_api_override(
        self,
        mock_urlopen,
    ) -> None:
        def fake_urlopen(request, timeout):
            self.assertEqual(request.full_url, "https://runtime.example.com/stocks.json")
            return _FakeUrlopenResponse('["300750"]')

        mock_urlopen.side_effect = fake_urlopen

        with tempfile.TemporaryDirectory() as temp_dir:
            env_path = Path(temp_dir) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "STOCK_LIST=600519",
                        "STOCK_LIST_FETCH_API=https://file.example.com/stocks.json",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            config = Config(stock_list=["600519"])

            with patch.dict(
                os.environ,
                {
                    "ENV_FILE": str(env_path),
                    "STOCK_LIST_FETCH_API": "https://runtime.example.com/stocks.json",
                },
                clear=True,
            ):
                Config._capture_bootstrap_runtime_env_overrides()
                config.refresh_stock_list()

        self.assertEqual(config.stock_list, ["300750"])
        self.assertEqual(config.stock_list_fetch_api, "https://runtime.example.com/stocks.json")

    def test_parse_report_language_accepts_known_alias_without_warning(self) -> None:
        with self.assertNoLogs("src.config", level="WARNING"):
            parsed = Config._parse_report_language("zh-cn")

        self.assertEqual(parsed, "zh")

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_invalid_numeric_env_values_fall_back_to_defaults(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        env = {
            "AGENT_ORCHESTRATOR_TIMEOUT_S": "oops",
            "NEWS_MAX_AGE_DAYS": "bad",
            "MAX_WORKERS": "",
            "WEBUI_PORT": "invalid",
        }

        with patch.dict(os.environ, env, clear=True):
            config = Config._load_from_env()

        self.assertEqual(config.agent_orchestrator_timeout_s, 600)
        self.assertEqual(config.news_max_age_days, 3)
        self.assertEqual(config.max_workers, 3)
        self.assertEqual(config.webui_port, 8000)

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_stock_email_groups_support_case_insensitive_env_names(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        env = {
            "STOCK_LIST": "600519,300750",
            "Stock_Group_1": "600519",
            "Email_Group_1": "user1@example.com",
            "stock_group_2": "300750",
            "email_group_2": "user2@example.com",
        }

        with patch.dict(os.environ, env, clear=True):
            config = Config._load_from_env()

        self.assertEqual(
            config.stock_email_groups,
            [
                (["600519"], ["user1@example.com"]),
                (["300750"], ["user2@example.com"]),
            ],
        )

    @patch("src.config.setup_env")
    @patch.object(Config, "_parse_litellm_yaml", return_value=[])
    def test_stock_email_groups_normalize_codes_at_parse_time(
        self,
        _mock_parse_yaml,
        _mock_setup_env,
    ) -> None:
        """STOCK_GROUP codes are canonicalized at parse time so that
        runtime email routing matches the same equivalence used in
        validate_structured()."""
        env = {
            "STOCK_LIST": "600519,HK00700",
            "STOCK_GROUP_1": "SH600519,1810.HK",
            "EMAIL_GROUP_1": "user@example.com",
        }

        with patch.dict(os.environ, env, clear=True):
            config = Config._load_from_env()

        stocks, emails = config.stock_email_groups[0]
        self.assertEqual(stocks, ["600519", "HK01810"])
        self.assertEqual(emails, ["user@example.com"])


if __name__ == "__main__":
    unittest.main()
