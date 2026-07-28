import asyncio
import unittest

from logger_utils.trace import (
    bind_trace_id,
    extract_trace_id,
    get_trace_id,
    parse_traceparent,
    reset_trace_id,
)


VALID_TRACE_ID = "0af7651916cd43dd8448eb211c80319c"
VALID_TRACEPARENT = (
    f"00-{VALID_TRACE_ID}-b7ad6b7169203331-01"
)


class TraceTests(unittest.TestCase):
    def test_valid_traceparent_returns_full_trace_id(self):
        self.assertEqual(parse_traceparent(VALID_TRACEPARENT), VALID_TRACE_ID)

    def test_invalid_traceparents_are_rejected(self):
        invalid = [
            "00-xyz-bad-99",
            "00-" + "0" * 32 + "-b7ad6b7169203331-01",
            "00-" + VALID_TRACE_ID + "-" + "0" * 16 + "-01",
            "ff-" + VALID_TRACE_ID + "-b7ad6b7169203331-01",
            "00-" + VALID_TRACE_ID.upper() + "-b7ad6b7169203331-01",
        ]
        for value in invalid:
            with self.subTest(value=value):
                self.assertIsNone(parse_traceparent(value))

    def test_custom_header_is_validated(self):
        self.assertEqual(
            extract_trace_id({"X-Trace-Id": "request_123"}), "request_123"
        )
        self.assertIsNone(extract_trace_id({"X-Trace-Id": "bad\ninjection"}))
        self.assertIsNone(extract_trace_id({"X-Trace-Id": "x" * 65}))

    def test_invalid_custom_header_falls_back_to_traceparent(self):
        self.assertEqual(
            extract_trace_id(
                {
                    "X-Trace-Id": "bad\ninjection",
                    "traceparent": VALID_TRACEPARENT,
                }
            ),
            VALID_TRACE_ID,
        )

    def test_token_restores_parent_context(self):
        parent_id, parent_token = bind_trace_id("parent")
        try:
            _, child_token = bind_trace_id("child")
            self.assertEqual(get_trace_id(), "child")
            reset_trace_id(child_token)
            self.assertEqual(get_trace_id(), parent_id)
        finally:
            reset_trace_id(parent_token)

    def test_async_tasks_have_isolated_contexts(self):
        async def run():
            parent_id, token = bind_trace_id("parent")
            try:
                async def child(index):
                    _, child_token = bind_trace_id(f"child-{index}")
                    try:
                        await asyncio.sleep(0)
                        return get_trace_id()
                    finally:
                        reset_trace_id(child_token)

                values = await asyncio.gather(*(child(i) for i in range(5)))
                return parent_id, values, get_trace_id()
            finally:
                reset_trace_id(token)

        parent_id, values, final_id = asyncio.run(run())
        self.assertEqual(values, [f"child-{i}" for i in range(5)])
        self.assertEqual(final_id, parent_id)


if __name__ == "__main__":
    unittest.main()
