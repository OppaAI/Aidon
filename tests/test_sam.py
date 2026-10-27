"""SAM 3.1 parsing and client, for video editing."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.capabilities.sam import FakeSAMClient, SAMClient, build_object_prompt, parse_sam_output


def test_parse_sam_output():
    segments = parse_sam_output(FakeSAMClient.WIRE)
    assert len(segments) == 2
    assert segments[0].object_id == "0"
    assert segments[0].frame == 0
    assert segments[0].box["x1"] == 10
    assert segments[0].confidence == 0.92
    assert segments[1].object_id == "1"


def test_parse_malformed_lines_skipped():
    assert parse_sam_output("not a sam line\n<0f>0<|box;x1=1;y1=2;x2=3;y2=4;w=10;h=10|>") != []


def test_fake_client_and_prompt():
    result = FakeSAMClient().segment(["frame.jpg"], "red toolbox")
    assert len(result["segments"]) == 2
    assert "red toolbox" in build_object_prompt("red toolbox")
    assert "box[1,2,3,4]" in build_object_prompt("red toolbox", [{"x1": 1, "y1": 2, "x2": 3, "y2": 4}])


def test_sam_client_fake_post():
    def fake_post(url, headers, body):
        assert url.endswith("/responses")
        assert body["model"] == "meta-sam"
        return {"output": [{"type": "message", "content": [{"type": "output_text", "text": FakeSAMClient.WIRE}]}]}
    client = SAMClient("https://api.meta.ai/v1", "test-key", post=fake_post)
    result = client.segment(["frame.jpg"], "red toolbox")
    assert "error" not in result
    assert len(result["segments"]) == 2


def test_sam_client_no_key():
    client = SAMClient("https://api.meta.ai/v1", "")
    result = client.segment(["frame.jpg"], "red toolbox")
    assert "error" in result
