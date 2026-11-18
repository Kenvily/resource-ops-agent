from app.core.rate_limit import RequestLimiter


def test_token_bucket_refills_and_bounds_client_memory():
    limiter = RequestLimiter(60, 2)
    assert limiter.allow("client", 0)
    assert limiter.allow("client", 0)
    assert not limiter.allow("client", 0)
    assert limiter.allow("client", 1)
    for i in range(1000):
        limiter.allow(str(i), 1)
    assert len(limiter.clients) == 512


async def test_http_rate_limit_returns_retry_after():
    import httpx

    from app.core.config import Settings
    from app.main import create_app

    app = create_app(Settings(_env_file=None, api_request_burst=5, api_requests_per_minute=10))
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client,
    ):
        for _ in range(5):
            assert (await client.get("/api/v1/insights/capabilities")).status_code == 200
        response = await client.get("/api/v1/insights/capabilities")
        assert response.status_code == 429 and response.headers["Retry-After"] == "2"
