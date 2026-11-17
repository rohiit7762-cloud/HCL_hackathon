import httpx

base = "http://127.0.0.1:8000"
users = httpx.get(base + "/api/v1/demo/users", timeout=10).raise_for_status().json()
person = next(user for user in users if user["employee_id"] == "asha")
session = httpx.post(base + "/api/v1/demo/session", json={"employee_id": person["employee_id"]}, timeout=10).raise_for_status().json()
result = httpx.post(
    base + "/api/v1/query",
    headers={"Authorization": f"Bearer {session['access_token']}"},
    json={"query": "How do I configure a service?"},
    timeout=90,
).raise_for_status().json()
print(f"Live API query completed; tasks={len(result['tasks'])}, citations={len(result['citations'])}.")
