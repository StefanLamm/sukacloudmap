"""独立进程 HTTP 验证；仅创建临时数据库，不改本地业务资料。"""
from pathlib import Path
from io import BytesIO
from openpyxl import load_workbook
from tempfile import TemporaryDirectory
import json
import os
import socket
import subprocess
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
PASSWORD = "Live-Test-Only-Password-9842!"


def main():
    with TemporaryDirectory(prefix="suka-live-") as folder:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        env = {**os.environ, "SUKA_DATABASE_URL": f"sqlite:///{folder}/live.db", "SUKA_PUBLIC_ORIGIN": origin,
               "SUKA_BOOTSTRAP_PASSWORD": PASSWORD, "SUKA_ENVIRONMENT": "test", "SUKA_MAP_AUTO_GEOCODE_ENABLED": "false"}
        initialized = subprocess.run([sys.executable, "-m", "app.cli", "init-admin"], cwd=ROOT, env=env, capture_output=True)
        assert initialized.returncode == 0, "临时管理员初始化失败"
        env.pop("SUKA_BOOTSTRAP_PASSWORD")
        process = None

        def start():
            nonlocal process
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "127.0.0.1", "--port", str(port), "--no-access-log"],
                                       cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("临时服务未能启动")
                try:
                    if httpx.get(origin + "/healthz", timeout=.3, trust_env=False).status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                time.sleep(.1)
            raise RuntimeError("临时服务启动超时")

        def stop():
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

        try:
            start()
            with httpx.Client(base_url=origin, trust_env=False) as admin, httpx.Client(base_url=origin, trust_env=False) as one, httpx.Client(base_url=origin, trust_env=False) as two:
                def login(client, username):
                    r = client.post("/api/auth/login", json={"username": username, "password": PASSWORD})
                    assert r.status_code == 200
                    client.headers["X-CSRF-Token"] = r.json()["csrf_token"]
                login(admin, "admin")
                for username in ["member_one", "member_two"]:
                    assert admin.post("/api/accounts", json={"username": username, "display_name": username, "password": PASSWORD}).status_code == 201
                login(one, "member_one"); login(two, "member_two")
                record = one.post("/api/workspace/records", json={"title": "独立进程重启验证", "content": "两个业务账号共享并持久保存"})
                assert record.status_code == 201
                assert two.get("/api/workspace/records").json()["items"][0]["id"] == record.json()["id"]
                def create(kind, payload):
                    result = one.post("/api/" + kind, json=payload)
                    assert result.status_code == 201, result.text
                    return result.json()
                warehouse = create("warehouses", {"code": "W01", "name": "重启测试仓", "address": "测试地址", "departure_time": "08:00"})
                other_warehouse = create("warehouses", {"code": "W02", "name": "当日测试仓", "address": "测试地址", "departure_time": "09:00"})
                quote = create("quotes", {"code": "Q01", "name": "重启测试价", "mode": "KM", "per_km": "2.00", "effective_from": "2026-11-11"})
                assert two.patch("/api/quotes/" + quote["id"], json={"expected_revision": 1, "per_km": "3", "effective_from": "2026-11-12"}).status_code == 200
                store = create("stores", {"code": "S01", "name": "重启测试店", "address": "测试地址", "warehouse_id": warehouse["id"],
                                  "business_open": "08:00", "business_close": "18:00", "unload_minutes": 0,
                                  "dedicated_quote_id": quote["id"], "common_unit_price": "0"})
                vehicle = create("vehicles", {"plate": "苏A12345", "warehouse_id": warehouse["id"], "max_pieces": 100, "available": True})
                assert two.put("/api/vehicles/" + vehicle["id"] + "/days/2026-11-11", json={"warehouse_id": other_warehouse["id"], "available": False, "expected_revision": 0}).status_code == 200
                catalog_path = "/api/master-data/catalog?delivery_date=2026-11-11"
                before = one.get(catalog_path).json()
                assert two.get(catalog_path).json() == before
                assert before["quotes"][0]["effective_version"]["per_km"] == "2"
                assert before["stores"][0]["common_unit_price"] == "0"
                assert before["vehicles"][0]["day_warehouse_id"] == other_warehouse["id"]
                assert before["vehicles"][0]["day_available"] is False
                wb = load_workbook(BytesIO(one.get("/api/planning/demand-template.xlsx").content))
                sheet = wb['数据']
                sheet.append([store['name'], 2, store['code']]); sheet.append([store['name'], 3, store['code']])
                stream = BytesIO(); wb.save(stream); wb.close()
                uploaded = one.post('/api/planning/demand-imports/upload?delivery_date=2026-11-11&filename=daily.xlsx', content=stream.getvalue())
                assert uploaded.status_code == 200
                import_id = uploaded.json()['id']
                preview = one.post('/api/planning/demand-imports/' + import_id + '/preview', json={'store_name': 0, 'pieces': 1, 'store_code': 2})
                assert preview.status_code == 200 and preview.json()['result']['after']['total_pieces'] == 5
                committed = one.post('/api/planning/demand-imports/' + import_id + '/commit', json={'delivery_date': '2026-11-11', 'preview_revision': 1})
                assert committed.status_code == 200
                snapshot_id = committed.json()['snapshot_id']
                snapshot_path = '/api/planning/snapshots/' + snapshot_id
                frozen = one.get(snapshot_path).json()['input']
                assert frozen['totals']['total_pieces'] == 5
                assert two.put('/api/planning/days/2026-11-11/stores/' + store['id'], json={'expected_revision': 0, 'latest_arrival_at': '2026-11-11T11:00:00+08:00'}).status_code == 200
                daily_path = '/api/planning/days/2026-11-11'
                changed = one.get(daily_path).json()
                assert changed['latest_snapshot']['input_changed'] is True
                updated_snapshot = one.post(daily_path + '/snapshots', json={'expected_revision': changed['revision'], 'expected_fingerprint': changed['input_fingerprint']})
                assert updated_snapshot.status_code == 200 and updated_snapshot.json()['version'] == 2
                daily_before = two.get(daily_path).json()
                created = one.post('/api/planning/jobs',json={'snapshot_id':updated_snapshot.json()['id'],'expected_head':0})
                assert created.status_code == 202, created.text
                job_id = created.json()['id']
                deadline = time.monotonic()+10
                while time.monotonic()<deadline:
                    job = two.get('/api/planning/jobs/'+job_id).json()
                    if job['status'] not in ['QUEUED','RUNNING']:break
                    time.sleep(.05)
                assert job['status']=='COMPLETED',job
                plan_path='/api/planning/plans/'+job['plan_id']
                planned=one.get(plan_path).json()
                assert planned['result']['check']['valid'] and planned['result']['total_fee']=='0.00'
                confirmed=two.post(plan_path+'/confirm',json={'expected_head':1})
                assert confirmed.status_code==201 and confirmed.json()['version']==2
                plan_export=one.get(plan_path+'/export.xlsx').content
                assert len(load_workbook(BytesIO(plan_export)).sheetnames)==6
                before = one.get(catalog_path).json()
                first_pid = process.pid
                stop(); start()
                assert first_pid != process.pid
                assert one.get("/api/auth/me").status_code == 200
                assert two.get("/api/workspace/records").json()["items"][0]["id"] == record.json()["id"]
                assert len(admin.get("/api/accounts").json()["items"]) == 3
                assert two.get(catalog_path).json() == before
                assert len(one.get("/api/quotes/" + quote["id"] + "/versions").json()["items"]) == 2
                assert admin.get("/").status_code == 200
                assert one.get("/api/accounts").status_code == 403
                assert two.get(daily_path).json() == daily_before
                assert two.get(snapshot_path).json()['input'] == frozen
                assert two.get('/api/planning/snapshots/' + updated_snapshot.json()['id']).json()['input']['stores'][0]['latest_arrival_at'] == '2026-11-11T11:00:00+08:00' 
                assert two.get(plan_path).json()['content_hash']==planned['content_hash']
                assert two.get(plan_path).json()['result']==planned['result']
                assert two.get('/api/planning/days/2026-11-11/plans').json()['total']==2
                assert one.get('/api/planning/jobs/'+job_id).json()['status']=='COMPLETED'
                print(json.dumps({"result": "PASS", "transport": "real HTTP / independent uvicorn processes", "database": "temporary file SQLite",
                                  "checks": ["bootstrap", "login", "two_business_accounts_share_records", "two_business_accounts_share_master_data", "future_quote_keeps_current_price", "zero_price_preserved", "vehicle_day_override_preserved", "new_process_after_restart", "sessions_survive_restart", "accounts_and_records_survive_restart", "master_catalog_identical_after_restart", "immutable_quote_versions_survive_restart", "business_user_admin_api_denied", "built_frontend_http_200", "daily_demand_aggregate_and_replace", "two_accounts_share_daily_input", "per_day_deadline_persists", "input_change_marks_old_snapshot", "two_immutable_snapshots_survive_restart", "current_input_fingerprint_survives_restart", "async_planning_job_completed", "common_only_plan_independently_verified", "confirmation_appends_new_version", "six_sheet_excel_export", "plan_hash_costs_and_jobs_survive_restart"],
                                  "production_data_modified": False}, ensure_ascii=False))
        finally:
            stop()


if __name__ == "__main__":
    main()
