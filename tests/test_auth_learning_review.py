"""账号登录、管理员门禁与学习审核：自动流程只到校验，批准/发布必须管理员手动。"""
import asyncio
import datetime as dt

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.application.learning_review import LearningReviewService, next_midnight, run_daily
from app.infrastructure.account_store import AccountLocked, AccountStore, MAX_FAILURES
from app.infrastructure.capability_registry import CapabilityRegistry
from app.infrastructure.identity import IdentityPolicy
from app.infrastructure.learning_store import LearningStore
from app.presentation.auth import register_auth_routes
from tests.test_learning_pipeline import Extractor, draft, enqueue, evidence

SECRET = "s" * 40
PASSWORD = "correct-horse-battery"


def build(tmp_path, *, mode="hmac", admins=("ycx",), extractor=None):
    store = LearningStore(tmp_path / "runs.db")
    accounts = AccountStore(tmp_path / "accounts.db")
    accounts.set_password("ycx", PASSWORD)
    accounts.set_password("buyer", PASSWORD)
    service = LearningReviewService(store, lambda: CapabilityRegistry(tmp_path / "cap.db"),
                                    (lambda: extractor) if extractor else None, report_path=tmp_path / "last.json")
    app = FastAPI()
    app.state.identity_policy = IdentityPolicy(mode=mode, secret=SECRET if mode == "hmac" else "")
    app.state.learning_admin_buyers = admins
    register_auth_routes(app, lambda: accounts, lambda: service)
    return TestClient(app), store, service


def login(client, user="ycx", password=PASSWORD):
    return client.post("/commerce/auth/login", json={"username": user, "password": password})


def auth(token):
    return {"Authorization": "Bearer " + token}


def test_password_store_hashes_and_locks(tmp_path):
    accounts = AccountStore(tmp_path / "a.db", clock=lambda: 1000.0)
    with pytest.raises(ValueError):
        accounts.set_password("ycx", "short")
    accounts.set_password("ycx", PASSWORD)
    assert PASSWORD.encode() not in (tmp_path / "a.db").read_bytes()
    assert accounts.verify("ycx", PASSWORD) and not accounts.verify("ghost", PASSWORD)
    for _ in range(MAX_FAILURES):
        assert not accounts.verify("ycx", "wrong-password-x")
    with pytest.raises(AccountLocked):
        accounts.verify("ycx", PASSWORD)


def test_login_issues_token_and_admin_gate(tmp_path):
    client, _, _ = build(tmp_path)
    assert login(client, password="wrong-password-x").status_code == 401
    assert login(client, user="ghost").status_code == 401
    admin = login(client).json()
    assert admin["is_learning_admin"] is True and admin["buyer_id"] == "ycx"
    buyer = login(client, user="buyer").json()
    assert buyer["is_learning_admin"] is False
    assert client.get("/commerce/auth/me", headers=auth(admin["access_token"])).json()["is_learning_admin"] is True
    assert client.get("/commerce/learning-review").status_code == 401
    assert client.get("/commerce/learning-review", headers=auth("forged.token.x")).status_code == 401
    assert client.get("/commerce/learning-review", headers=auth(buyer["access_token"])).status_code == 403
    assert client.get("/commerce/learning-review", headers=auth(admin["access_token"])).status_code == 200


def test_demo_mode_has_no_login_or_review(tmp_path):
    client, _, _ = build(tmp_path, mode="demo")
    assert login(client).status_code == 404
    assert client.get("/commerce/learning-review").status_code == 503


@pytest.mark.asyncio
async def test_cycle_validates_but_never_approves_or_publishes(tmp_path):
    _, store, service = build(tmp_path, extractor=Extractor(draft(keywords=["出行"])))
    # 来源请求含关键词“出行”，可触发；无关探针不触发。
    enqueue(store)
    report = await service.run_cycle(trigger="schedule")
    assert report["validated"] == 1 and report["auto_approved"] == 0 and report["auto_published"] == 0
    [candidate] = store.list_records("strategy")
    assert candidate["status"] == "validated"
    assert candidate["validation"]["validator_version"] == "auto-structural-v1"
    assert service.last_report()["trigger"] == "schedule"
    assert CapabilityRegistry(tmp_path / "cap.db").lookup_strategies("出行买背包") == []


@pytest.mark.asyncio
async def test_overbroad_or_untriggered_keywords_fail_validation(tmp_path):
    _, store, service = build(tmp_path, extractor=Extractor(draft(keywords=["订单"])))
    enqueue(store, evidence(input="出行前收到防水背包，订单要快"))
    report = await service.run_cycle(trigger="manual")
    assert report["validated"] == 0 and report["validation_failed"] == 1
    [candidate] = store.list_records("strategy")
    assert candidate["status"] == "captured"
    check = service.overview()["candidates"][0]["pending_check"]
    assert check["passed"] is False and "关键词过宽" in check["cases"][1]["reason"]


@pytest.mark.asyncio
async def test_cycle_lock_rejects_concurrent_runs(tmp_path):
    _, _, service = build(tmp_path)
    async with service._lock:
        with pytest.raises(RuntimeError):
            await service.run_cycle(trigger="manual")


def test_admin_review_publish_withdraw_flow(tmp_path):
    client, store, service = build(tmp_path, extractor=Extractor(draft(keywords=["出行"])))
    enqueue(store)
    asyncio.run(service.run_cycle(trigger="manual"))
    headers = auth(login(client).json()["access_token"])
    candidate = client.get("/commerce/learning-review", headers=headers).json()["candidates"][0]
    assert candidate["status"] == "validated"
    path = "/commerce/learning-review/candidates/" + candidate["id"]
    # 未批准不能发布。
    assert client.post(path + "/publish", headers=headers, json={"expected_hash": candidate["content_hash"]}).status_code == 409
    stale = {"accepted": True, "expected_hash": "0" * 64, "expected_revision": 1, "note": "ok"}
    assert client.post(path + "/review", headers=headers, json=stale).status_code == 409
    body = {"accepted": True, "expected_hash": candidate["content_hash"], "expected_revision": 1, "note": "正反例合理"}
    assert client.post(path + "/review", headers=headers, json=body).json()["status"] == "approved"
    assert store.get("strategy", candidate["id"])["reviewer"] == "ycx"
    published = client.post(path + "/publish", headers=headers, json={"expected_hash": candidate["content_hash"], "expires_days": 30})
    assert published.status_code == 200
    assert CapabilityRegistry(tmp_path / "cap.db").lookup_strategies("出行买背包")
    assert client.post(path + "/withdraw", headers=headers, json={"reason": "测试撤回"}).json()["status"] == "withdrawn"
    assert CapabilityRegistry(tmp_path / "cap.db").lookup_strategies("出行买背包") == []


def test_next_midnight_shanghai():
    noon = dt.datetime(2026, 10, 23, 12, 0, tzinfo=dt.timezone(dt.timedelta(hours=8))).timestamp()
    target = dt.datetime.fromtimestamp(next_midnight(noon, "Asia/Shanghai"), dt.timezone(dt.timedelta(hours=8)))
    assert (target.year, target.month, target.day, target.hour, target.minute) == (2026, 10, 24, 0, 0)


@pytest.mark.asyncio
async def test_daily_loop_survives_failure(tmp_path):
    calls = []

    class Service:
        async def run_cycle(self, *, trigger):
            calls.append(trigger)
            raise ValueError("boom")

    async def sleep(_):
        if len(calls) >= 2:
            raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await run_daily(Service(), "Asia/Shanghai", sleep=sleep)
    assert calls == ["schedule", "schedule"]


def test_login_validation_error_does_not_echo_password(tmp_path):
    client, _, _ = build(tmp_path)
    response = client.post("/commerce/auth/login", json={"buyer_id": "ycx", "password": "secret-should-not-echo"})
    assert response.status_code == 422
    assert "secret-should-not-echo" not in response.text


@pytest.mark.asyncio
async def test_history_keeps_earlier_runs_and_job_totals(tmp_path):
    _, store, service = build(tmp_path, extractor=Extractor(draft(keywords=["出行"])))
    # 上线前只存最后一轮报告：首次写历史时要并入，不能丢。
    (tmp_path / "last.json").write_text('{"trigger":"manual","started_at":1,"finished_at":2,"jobs":{"diagnostic_saved":2},"validated":0,"validation_failed":0}', encoding="utf-8")
    enqueue(store)
    await service.run_cycle(trigger="manual")
    await service.run_cycle(trigger="schedule")
    history = service.history()
    assert [item["trigger"] for item in history] == ["manual", "manual", "schedule"]
    assert history[0]["jobs"] == {"diagnostic_saved": 2} and history[-1]["jobs"] == {}
    totals = service.job_totals()
    assert totals["total"] == 1 and totals["pending"] == 0
    assert totals["outcomes"] == {"candidate_captured": 1} and totals["last_processed_at"]
