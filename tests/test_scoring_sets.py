"""scoring_platform 动作卡片标注接口测试(完全独立于采集系统)。

模型:每个被试按 part_sets.xlsx 预创建 11 张固定卡片(warm_up1-3 + 表格 8 动作),
保存 = 按 label upsert 卡片边界与分数,删除 = 清除卡片数据保留卡片。
"""

import asyncio
import csv
import json
import shutil
from bisect import bisect_left
from pathlib import Path

import httpx
import pytest

from backend import create_app

DATE = "2026_8_18"
SUBJECT = "FAKE01"
SUBJECT2 = "FAKE02"
FPS = 10.0
CAM2_OFFSET = 0.05
START = 1787030382.0
RAW_SIZE = 640 * 480 * 3
TABLE = {
    SUBJECT: ["A1T", "A1C", "B1T", "B1C", "A2T", "A2C", "B2T", "B2C"],
    SUBJECT2: ["B1T", "B1C", "A2T", "A2C", "B2T", "B2C", "A1T", "A1C"],
}
SEQUENCE = ["warm_up1", "warm_up2", "warm_up3"] + TABLE[SUBJECT]
SETS_URL = f"/api/subjects/{DATE}/{SUBJECT}/sets"


def write_trial(root: Path, date: str = DATE, subject: str = SUBJECT, frames: int = 100, with_raw: bool = False) -> Path:
    trial = root / date / subject
    ts_dir = trial / "timestamps"
    color_dir = trial / "color"
    ts_dir.mkdir(parents=True)
    if with_raw:
        color_dir.mkdir(parents=True)
    for camera_id in ("cam1", "cam2"):
        offset = 0.0 if camera_id == "cam1" else CAM2_OFFSET
        rows = []
        for index in range(frames):
            timestamp = START + index / FPS + offset
            raw_path = f"color/{camera_id}_color_rgb_{index}_0us.raw"
            rows.append({
                "frame_index": index,
                "timestamp": f"{timestamp:.6f}",
                "color_width": 640,
                "color_height": 480,
                "color_raw_path": raw_path,
            })
            if with_raw:
                (color_dir / f"{camera_id}_color_rgb_{index}_0us.raw").write_bytes(b"\x00" * RAW_SIZE)
        with (ts_dir / f"{camera_id}_rgbd_timestamps.csv").open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=["frame_index", "timestamp", "color_width", "color_height", "color_raw_path"])
            writer.writeheader()
            writer.writerows(rows)
    return trial


def write_part_sets(root: Path, subjects: dict[str, list[str]]) -> Path:
    """写动作序列表 JSON(与提交的 docs/part_sets.json 同构)。"""
    path = root / "part_sets.json"
    payload = {"schema_version": "1.0.0", "source": "test", "subjects": subjects}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def camera_timestamps(trial: Path, camera_id: str) -> list[float]:
    with (trial / "timestamps" / f"{camera_id}_rgbd_timestamps.csv").open(encoding="utf-8") as handle:
        return [float(row["timestamp"]) for row in csv.DictReader(handle)]


def nearest_index(timestamps: list[float], target: float) -> int:
    index = bisect_left(timestamps, target)
    if index >= len(timestamps):
        return len(timestamps) - 1
    if index > 0:
        before = timestamps[index - 1]
        after = timestamps[index]
        if target - before <= after - target:
            index -= 1
    return index


def make_app(root: Path, with_table: bool = True, corrupt_table: bool = False):
    part_sets_path = root / "part_sets.json"
    if with_table:
        write_part_sets(root, TABLE)
    if corrupt_table:
        part_sets_path.write_bytes(b"{not json")
    return create_app(
        data_root=root,
        cache_root=root / "cache",
        annotations_root=root / "annotations",
        part_sets_path=part_sets_path,
    )


def make_client(root: Path, **kwargs) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=make_app(root, **kwargs)), base_url="http://test")


def read_annotations(root: Path) -> dict:
    return json.loads((root / "annotations" / "score_annotation.json").read_text(encoding="utf-8"))


def test_sets_seeds_11_cards_on_first_get(tmp_path):
    write_trial(tmp_path)

    async def scenario():
        async with make_client(tmp_path) as client:
            first = await client.get(SETS_URL)
            assert first.status_code == 200
            payload = first.json()
            assert payload["schema_version"] == "1.1.0"
            assert [item["label"] for item in payload["sets"]] == SEQUENCE
            assert [item["id"] for item in payload["sets"]] == list(range(1, 12))
            for card in payload["sets"]:
                assert card["start_position"] is None and card["score"] is None and card["cameras"] == {}
            on_disk = read_annotations(tmp_path)
            assert on_disk["schema_version"] == "1.1.0"
            assert len(on_disk["subjects"][f"{DATE}/{SUBJECT}"]["sets"]) == 11
            second = await client.get(SETS_URL)
            assert len(second.json()["sets"]) == 11  # 幂等,不重复播种

    asyncio.run(scenario())


def test_save_card_persists_schema_and_boundaries(tmp_path):
    trial = write_trial(tmp_path)
    cam1_ts = camera_timestamps(trial, "cam1")
    cam2_ts = camera_timestamps(trial, "cam2")
    timeline_start = max(cam1_ts[0], cam2_ts[0])
    timeline_end = min(cam1_ts[-1], cam2_ts[-1])
    duration = timeline_end - timeline_start
    start_position, end_position = 1000, 4000

    async def scenario():
        async with make_client(tmp_path) as client:
            response = await client.post(
                SETS_URL,
                json={"label": "A1T", "start_position": start_position, "end_position": end_position, "score": 3},
            )
            assert response.status_code == 200  # 播种后 upsert
            record = response.json()["set"]
            start_target = timeline_start + duration * start_position / 10000
            end_target = timeline_start + duration * end_position / 10000
            assert record["id"] == SEQUENCE.index("A1T") + 1
            assert record["label"] == "A1T"
            assert record["start_position"] == start_position
            assert record["end_position"] == end_position
            assert record["start_sec"] == pytest.approx(start_target, abs=1e-6)
            assert record["end_sec"] == pytest.approx(end_target, abs=1e-6)
            assert record["duration_sec"] == pytest.approx(end_target - start_target, abs=1e-6)
            assert record["score"] == 3
            for camera_id, timestamps in (("cam1", cam1_ts), ("cam2", cam2_ts)):
                for side, target in (("start", start_target), ("end", end_target)):
                    expected_index = nearest_index(timestamps, target)
                    boundary = record["cameras"][camera_id][side]
                    assert boundary["frame_index"] == expected_index
                    assert boundary["timestamp"] == pytest.approx(timestamps[expected_index], abs=1e-6)
            offset = record["cameras"]["cam1"]["start"]["inter_camera_offset_us"]
            assert offset == -record["cameras"]["cam2"]["start"]["inter_camera_offset_us"]
            assert record["created_at"] and record["updated_at"]
            on_disk = read_annotations(tmp_path)
            stored = on_disk["subjects"][f"{DATE}/{SUBJECT}"]["sets"]
            assert len(stored) == 11
            assert stored[SEQUENCE.index("A1T")] == record
            assert all(item["start_position"] is None for item in stored if item["label"] != "A1T")

    asyncio.run(scenario())


def test_save_card_validation_errors(tmp_path):
    write_trial(tmp_path)

    async def scenario():
        async with make_client(tmp_path) as client:
            bad_bodies = [
                {"start_position": 5000, "end_position": 5000, "label": "A1T"},
                {"start_position": 6000, "end_position": 4000, "label": "A1T"},
                {"start_position": -1, "end_position": 4000, "label": "A1T"},
                {"start_position": 1000, "end_position": 10001, "label": "A1T"},
                {"start_position": "abc", "end_position": 4000, "label": "A1T"},
                {"start_position": True, "end_position": 4000, "label": "A1T"},
                {"start_position": 1000, "end_position": 4000, "label": "A1T", "score": "abc"},
                {"start_position": 1000, "end_position": 4000, "label": "A1T", "score": 1.5},
                {"start_position": 1000, "end_position": 4000, "label": "A1T", "score": 6},
                {"start_position": 1000, "end_position": 4000, "label": "A1T", "score": -1},
                {"start_position": 1000, "end_position": 4000, "label": "A1T", "score": True},
                {"start_position": 1000, "end_position": 4000, "label": "A1Z"},
                {"start_position": 1000, "end_position": 4000},
                {"start_position": 1000, "end_position": 4000, "label": "x" * 65},
                {"start_position": 1000, "end_position": 4000, "label": 5},
                {},
            ]
            for body in bad_bodies:
                response = await client.post(SETS_URL, json=body)
                assert response.status_code == 400, f"{body} -> {response.status_code}: {response.text}"
            assert (await client.post(SETS_URL)).status_code == 400
            assert (await client.post(SETS_URL, content=b"[1, 2]")).status_code == 400
            ok = await client.post(SETS_URL, json={"label": "A2T", "start_position": 1000, "end_position": 4000, "score": None})
            assert ok.status_code == 200
            assert ok.json()["set"]["score"] is None

    asyncio.run(scenario())


def test_warmup_cards_unscored(tmp_path):
    write_trial(tmp_path)

    async def scenario():
        async with make_client(tmp_path) as client:
            scored = await client.post(SETS_URL, json={"label": "warm_up1", "start_position": 100, "end_position": 200, "score": 3})
            assert scored.status_code == 400 and "热身" in scored.json()["detail"]
            ok = await client.post(SETS_URL, json={"label": "warm_up1", "start_position": 100, "end_position": 200})
            assert ok.status_code == 200
            record = ok.json()["set"]
            assert record["score"] is None and record["start_position"] == 100
            patched = await client.patch(f"{SETS_URL}/{record['id']}", json={"score": 1})
            assert patched.status_code == 400 and "热身" in patched.json()["detail"]

    asyncio.run(scenario())


def test_clear_card_keeps_card(tmp_path):
    write_trial(tmp_path)

    async def scenario():
        async with make_client(tmp_path) as client:
            await client.post(SETS_URL, json={"label": "A1T", "start_position": 1000, "end_position": 2000, "score": 2})
            await client.post(SETS_URL, json={"label": "B1T", "start_position": 3000, "end_position": 4000})
            sets = (await client.get(SETS_URL)).json()["sets"]
            a1t_id = next(item["id"] for item in sets if item["label"] == "A1T")
            cleared = await client.delete(f"{SETS_URL}/{a1t_id}")
            assert cleared.status_code == 200
            record = cleared.json()["set"]
            assert record["id"] == a1t_id and record["label"] == "A1T"
            assert record["start_position"] is None and record["score"] is None and record["cameras"] == {}
            listing = (await client.get(SETS_URL)).json()["sets"]
            assert len(listing) == 11
            a1t = next(item for item in listing if item["label"] == "A1T")
            assert a1t["start_position"] is None
            assert next(item for item in listing if item["label"] == "B1T")["start_position"] == 3000
            assert (await client.delete(f"{SETS_URL}/999")).status_code == 404
            assert (await client.delete(f"{SETS_URL}/abc")).status_code == 404

    asyncio.run(scenario())


def test_atomic_write_no_part_left(tmp_path):
    write_trial(tmp_path)

    async def scenario():
        async with make_client(tmp_path) as client:
            await client.post(SETS_URL, json={"label": "A1T", "start_position": 1000, "end_position": 2000})
            annotations_root = tmp_path / "annotations"
            assert not (annotations_root / ".score_annotation.json.part").exists()
            assert len(read_annotations(tmp_path)["subjects"][f"{DATE}/{SUBJECT}"]["sets"]) == 11

    asyncio.run(scenario())


def test_corrupt_sets_file_tolerated(tmp_path):
    write_trial(tmp_path)
    annotations_root = tmp_path / "annotations"
    annotations_root.mkdir(parents=True)
    (annotations_root / "score_annotation.json").write_text("{not json", encoding="utf-8")

    async def scenario():
        async with make_client(tmp_path) as client:
            response = await client.get(SETS_URL)
            assert response.status_code == 200
            assert len(response.json()["sets"]) == 11  # 播种覆盖损坏文件
            on_disk = read_annotations(tmp_path)
            assert on_disk["schema_version"] == "1.1.0"
            assert len(on_disk["subjects"][f"{DATE}/{SUBJECT}"]["sets"]) == 11

    asyncio.run(scenario())


def test_boundary_frames_match_frame_preview(tmp_path):
    # WYSIWYG:卡片边界帧 == /frame 接口渲染给用户看的帧
    trial = write_trial(tmp_path, frames=6, with_raw=True)
    cam1_ts = camera_timestamps(trial, "cam1")
    cam2_ts = camera_timestamps(trial, "cam2")
    timeline_start = max(cam1_ts[0], cam2_ts[0])
    timeline_end = min(cam1_ts[-1], cam2_ts[-1])
    duration = timeline_end - timeline_start
    position = 3000

    async def scenario():
        async with make_client(tmp_path) as client:
            created = await client.post(SETS_URL, json={"label": "A1T", "start_position": position, "end_position": 7000})
            assert created.status_code == 200
            stored = created.json()["set"]["cameras"]["cam1"]["start"]
            frame = await client.get(f"/api/subjects/{DATE}/{SUBJECT}/frame/cam1?position={position}")
            assert frame.status_code == 200
            assert frame.headers["content-type"] == "image/jpeg"
            assert int(frame.headers["x-frame-index"]) == stored["frame_index"]
            assert float(frame.headers["x-frame-timestamp"]) == pytest.approx(stored["timestamp"], abs=1e-6)

    asyncio.run(scenario())


def test_concurrent_seed_and_save_serialized(tmp_path):
    write_trial(tmp_path)

    async def scenario():
        async with make_client(tmp_path) as client:
            first, second = await asyncio.gather(client.get(SETS_URL), client.get(SETS_URL))
            assert first.status_code == 200 and second.status_code == 200
            assert len(first.json()["sets"]) == len(second.json()["sets"]) == 11
            assert len(read_annotations(tmp_path)["subjects"]) == 1
            one, two = await asyncio.gather(
                client.post(SETS_URL, json={"label": "A1T", "start_position": 1000, "end_position": 2000, "score": 1}),
                client.post(SETS_URL, json={"label": "B2C", "start_position": 3000, "end_position": 4000, "score": 2}),
            )
            assert one.status_code == 200 and two.status_code == 200
            stored = read_annotations(tmp_path)["subjects"][f"{DATE}/{SUBJECT}"]["sets"]
            assert len(stored) == 11
            assert sum(item["start_position"] is not None for item in stored) == 2

    asyncio.run(scenario())


def test_patch_score(tmp_path):
    write_trial(tmp_path)

    async def scenario():
        async with make_client(tmp_path) as client:
            created = await client.post(SETS_URL, json={"label": "A2T", "start_position": 1000, "end_position": 2000, "score": 2})
            set_id = created.json()["set"]["id"]
            assert (await client.patch(f"{SETS_URL}/{set_id}", json={"score": 5})).json()["set"]["score"] == 5
            assert (await client.patch(f"{SETS_URL}/{set_id}", json={"score": None})).json()["set"]["score"] is None
            assert (await client.patch(f"{SETS_URL}/{set_id}", json={"score": 9})).status_code == 400
            assert (await client.patch(f"{SETS_URL}/{set_id}", json={})).status_code == 400
            assert (await client.patch(f"{SETS_URL}/999", json={"score": 3})).status_code == 404
            listing = (await client.get(SETS_URL)).json()["sets"]
            assert next(item for item in listing if item["id"] == set_id)["score"] is None

    asyncio.run(scenario())


def test_unknown_subject_404_and_non_table_subject(tmp_path):
    write_trial(tmp_path)
    write_trial(tmp_path, subject="GHOST01")  # 存在数据但不在表格中

    async def scenario():
        async with make_client(tmp_path) as client:
            assert (await client.get("/api/subjects/nope/subj/sets")).status_code == 404
            assert (await client.post("/api/subjects/nope/subj/sets", json={"label": "A1T", "start_position": 0, "end_position": 100})).status_code == 404
            assert (await client.delete("/api/subjects/nope/subj/sets/1")).status_code == 404
            assert (await client.patch("/api/subjects/nope/subj/sets/1", json={"score": 1})).status_code == 404
            # 表格外被试:GET 空、POST 拒绝、PATCH/DELETE 404
            ghost = (await client.get(f"/api/subjects/{DATE}/GHOST01/sets")).json()["sets"]
            assert ghost == []
            denied = await client.post(f"/api/subjects/{DATE}/GHOST01/sets", json={"label": "A1T", "start_position": 0, "end_position": 100})
            assert denied.status_code == 400 and "固定序列" in denied.json()["detail"]
            assert (await client.patch(f"/api/subjects/{DATE}/GHOST01/sets/1", json={"score": 1})).status_code == 404
            assert (await client.delete(f"/api/subjects/{DATE}/GHOST01/sets/1")).status_code == 404

    asyncio.run(scenario())


def test_sets_isolated_per_subject(tmp_path):
    write_trial(tmp_path, subject=SUBJECT)
    write_trial(tmp_path, subject=SUBJECT2)

    async def scenario():
        async with make_client(tmp_path) as client:
            await client.post(f"/api/subjects/{DATE}/{SUBJECT}/sets", json={"label": "A1T", "start_position": 0, "end_position": 1000})
            await client.post(f"/api/subjects/{DATE}/{SUBJECT2}/sets", json={"label": "B1T", "start_position": 2000, "end_position": 3000})
            first = (await client.get(f"/api/subjects/{DATE}/{SUBJECT}/sets")).json()["sets"]
            second = (await client.get(f"/api/subjects/{DATE}/{SUBJECT2}/sets")).json()["sets"]
            assert len(first) == len(second) == 11
            assert next(item for item in first if item["label"] == "A1T")["start_position"] == 0
            assert all(item["start_position"] is None for item in first if item["label"] != "A1T")
            assert next(item for item in second if item["label"] == "B1T")["start_position"] == 2000
            on_disk = read_annotations(tmp_path)
            assert set(on_disk["subjects"]) == {f"{DATE}/{SUBJECT}", f"{DATE}/{SUBJECT2}"}

    asyncio.run(scenario())


def test_sets_fixed_sequence_order(tmp_path):
    write_trial(tmp_path)

    async def scenario():
        async with make_client(tmp_path) as client:
            # 乱序保存、清除、重存:顺序恒为固定序列
            await client.post(SETS_URL, json={"label": "B2C", "start_position": 8000, "end_position": 9000, "score": 5})
            await client.post(SETS_URL, json={"label": "A1T", "start_position": 1000, "end_position": 2000, "score": 2})
            sets = (await client.get(SETS_URL)).json()["sets"]
            a1t_id = next(item["id"] for item in sets if item["label"] == "A1T")
            await client.delete(f"{SETS_URL}/{a1t_id}")
            await client.post(SETS_URL, json={"label": "A1T", "start_position": 1500, "end_position": 2500, "score": 3})
            listing = (await client.get(SETS_URL)).json()["sets"]
            assert [item["label"] for item in listing] == SEQUENCE
            assert [item["id"] for item in listing] == list(range(1, 12))
            assert next(item for item in listing if item["label"] == "A1T")["start_position"] == 1500

    asyncio.run(scenario())


def test_single_file_preserves_other_subjects(tmp_path):
    write_trial(tmp_path, subject=SUBJECT)
    write_trial(tmp_path, subject=SUBJECT2)

    async def scenario():
        async with make_client(tmp_path) as client:
            await client.post(f"/api/subjects/{DATE}/{SUBJECT}/sets", json={"label": "A1T", "start_position": 0, "end_position": 1000, "score": 1})
            await client.post(f"/api/subjects/{DATE}/{SUBJECT2}/sets", json={"label": "A1T", "start_position": 2000, "end_position": 3000, "score": 2})
            await client.post(f"/api/subjects/{DATE}/{SUBJECT}/sets", json={"label": "B1T", "start_position": 4000, "end_position": 5000, "score": 3})
            payload = read_annotations(tmp_path)
            assert len(payload["subjects"][f"{DATE}/{SUBJECT}"]["sets"]) == 11
            assert len(payload["subjects"][f"{DATE}/{SUBJECT2}"]["sets"]) == 11
            # PATCH 一个被试不影响另一个
            first_a1t = next(item for item in payload["subjects"][f"{DATE}/{SUBJECT}"]["sets"] if item["label"] == "A1T")
            await client.patch(f"/api/subjects/{DATE}/{SUBJECT}/sets/{first_a1t['id']}", json={"score": 5})
            payload = read_annotations(tmp_path)
            second_a1t = next(item for item in payload["subjects"][f"{DATE}/{SUBJECT2}"]["sets"] if item["label"] == "A1T")
            assert second_a1t["score"] == 2
            # 清除一个被试的卡片不影响另一个,且卡片保留
            await client.delete(f"/api/subjects/{DATE}/{SUBJECT2}/sets/{second_a1t['id']}")
            payload = read_annotations(tmp_path)
            assert len(payload["subjects"][f"{DATE}/{SUBJECT2}"]["sets"]) == 11
            cleared = next(item for item in payload["subjects"][f"{DATE}/{SUBJECT2}"]["sets"] if item["label"] == "A1T")
            assert cleared["start_position"] is None and cleared["score"] is None

    asyncio.run(scenario())


def test_missing_part_sets_tolerated(tmp_path):
    write_trial(tmp_path)

    async def scenario():
        async with make_client(tmp_path, with_table=False) as client:
            response = await client.get(SETS_URL)
            assert response.status_code == 200
            assert response.json()["sets"] == []
            health = await client.get("/api/health")
            assert health.json()["part_sets_loaded"] == 0

    asyncio.run(scenario())


def test_corrupt_part_sets_tolerated(tmp_path):
    write_trial(tmp_path)

    async def scenario():
        async with make_client(tmp_path, corrupt_table=True) as client:
            response = await client.get(SETS_URL)
            assert response.status_code == 200
            assert response.json()["sets"] == []
            assert (await client.get("/api/health")).json()["part_sets_loaded"] == 0

    asyncio.run(scenario())


def test_upsert_updates_existing_card(tmp_path):
    write_trial(tmp_path)

    async def scenario():
        async with make_client(tmp_path) as client:
            first = await client.post(SETS_URL, json={"label": "A1T", "start_position": 1000, "end_position": 2000, "score": 2})
            assert first.status_code == 200
            second = await client.post(SETS_URL, json={"label": "A1T", "start_position": 3000, "end_position": 4000, "score": 5})
            assert second.status_code == 200
            before, after = first.json()["set"], second.json()["set"]
            assert after["id"] == before["id"] and after["label"] == "A1T"
            assert after["created_at"] == before["created_at"]
            assert after["start_position"] == 3000 and after["score"] == 5
            assert second.json()["count"] == 11
            stored = read_annotations(tmp_path)["subjects"][f"{DATE}/{SUBJECT}"]["sets"]
            assert len(stored) == 11
            assert next(item for item in stored if item["label"] == "A1T") == after

    asyncio.run(scenario())


def test_startup_bulk_seeds_all_known_subjects(tmp_path):
    # 启动时批量预录入:data 中存在于表格的被试立即写入 JSON;表格外被试跳过;
    # 重启不覆盖已有标注(幂等)。
    write_trial(tmp_path, subject=SUBJECT)
    write_trial(tmp_path, subject=SUBJECT2)
    write_trial(tmp_path, subject="GHOST01")

    async def scenario():
        async with make_client(tmp_path) as client:
            on_disk = read_annotations(tmp_path)
            assert set(on_disk["subjects"]) == {f"{DATE}/{SUBJECT}", f"{DATE}/{SUBJECT2}"}
            assert all(len(entry["sets"]) == 11 for entry in on_disk["subjects"].values())
            await client.post(f"/api/subjects/{DATE}/{SUBJECT}/sets",
                              json={"label": "A1T", "start_position": 1000, "end_position": 2000, "score": 3})
        # 重新 create_app 模拟重启:已有标注保留,不重复播种
        async with make_client(tmp_path) as client:
            on_disk = read_annotations(tmp_path)
            assert len(on_disk["subjects"]) == 2
            a1t = next(item for item in on_disk["subjects"][f"{DATE}/{SUBJECT}"]["sets"] if item["label"] == "A1T")
            assert a1t["score"] == 3 and a1t["start_position"] == 1000
            listing = (await client.get(f"/api/subjects/{DATE}/{SUBJECT}/sets")).json()["sets"]
            assert len(listing) == 11

    asyncio.run(scenario())


def test_part_sets_json_loads_real_file():
    from backend import PLATFORM_ROOT
    real = PLATFORM_ROOT / "docs" / "part_sets.json"
    if not real.is_file():
        pytest.skip("docs/part_sets.json 不存在")
    table = json.loads(real.read_text(encoding="utf-8"))["subjects"]
    assert len(table) == 48
    for subject_id, codes in table.items():
        assert len(codes) == 8
        assert len(set(codes)) == 8
    assert set(table["CR12"]) == {"A1T", "A1C", "B1T", "B1C", "A2T", "A2C", "B2T", "B2C"}


def test_regression_frame_preview(tmp_path):
    write_trial(tmp_path, frames=6, with_raw=True)

    async def scenario():
        async with make_client(tmp_path) as client:
            response = await client.get(f"/api/subjects/{DATE}/{SUBJECT}/frame/cam1?position=5000")
            assert response.status_code == 200
            assert response.headers["content-type"] == "image/jpeg"
            assert int(response.headers["x-frame-index"]) >= 0
            assert float(response.headers["x-frame-timestamp"]) > 0
            assert response.content[:2] == b"\xff\xd8"

    asyncio.run(scenario())


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="需要 FFmpeg 生成预览视频")
def test_regression_preview_generation(tmp_path):
    write_trial(tmp_path, frames=20, with_raw=True)

    async def scenario():
        async with make_client(tmp_path) as client:
            created = await client.post(f"/api/subjects/{DATE}/{SUBJECT}/preview")
            assert created.status_code == 202
            status = None
            for _ in range(100):
                status = await client.get(f"/api/subjects/{DATE}/{SUBJECT}/preview/status")
                if status.json()["status"] != "generating":
                    break
                await asyncio.sleep(0.05)
            assert status is not None and status.json()["status"] == "ready", status.text
            for camera_id in ("cam1", "cam2"):
                video = tmp_path / "cache" / DATE / SUBJECT / f"{camera_id}_preview.mp4"
                assert video.is_file() and video.stat().st_size > 0

    asyncio.run(scenario())
