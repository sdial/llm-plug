ADMIN_TEST_PASSWORD = "test-admin-password"


async def login_admin(client, password: str = ADMIN_TEST_PASSWORD):
    setup = await client.post("/admin/auth/setup", json={"password": password})
    if setup.status_code not in (200, 409):
        raise AssertionError(f"admin setup failed: {setup.status_code} {setup.text}")
    login = await client.post("/admin/auth/login", json={"password": password})
    if login.status_code != 200:
        raise AssertionError(f"admin login failed: {login.status_code} {login.text}")
    csrf = await client.get("/admin/auth/csrf")
    if csrf.status_code != 200:
        raise AssertionError(f"admin csrf failed: {csrf.status_code} {csrf.text}")
    client.headers["X-CSRF-Token"] = csrf.json()["csrf_token"]
    return login
