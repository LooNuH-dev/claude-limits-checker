from app import report


def test_five_hour_parsing():
    p, dt = report.five_hour({"five_hour": {"utilization": 100, "resets_at": "2030-01-01T00:00:00Z"}})
    assert p == 100.0 and dt.year == 2030
    assert report.five_hour({}) == (0.0, None)


def test_status_escapes_and_errors():
    text = report.format_status([
        ("<b>evil</b>", {"five_hour": {"utilization": 10}, "seven_day": {"utilization": 20}}, None),
        ("broken", None, "HTTP 500"),
    ])
    assert "&lt;b&gt;evil" in text
    assert "10.0%" in text and "20.0%" in text
    assert "HTTP 500" in text


def test_status_empty():
    assert "Нет аккаунтов" in report.format_status([])


def test_clip_short_text_unchanged():
    assert report.clip("hello") == "hello"


def test_clip_truncates_long_text():
    text = "x" * 5000
    out = report.clip(text, limit=100)
    assert len(out) == 102
    assert out.endswith("\n…")
    assert out.startswith("x" * 100)
