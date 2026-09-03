import httpx
import respx

from solari_ci.client import SolariClient


@respx.mock
async def test_create_sandbox_retries_then_succeeds() -> None:
    route = respx.post("https://api.getsolari.com/sandboxes").mock(
        side_effect=[httpx.Response(503), httpx.Response(201, json={"sandboxId": "sb-1"})]
    )
    async with SolariClient(api_key="test-key") as client:
        result = await client.create_sandbox(cpu=2, mem_mb=1024)
    assert result == {"sandboxId": "sb-1"}
    assert route.call_count == 2


@respx.mock
async def test_exec_caps_timeout() -> None:
    route = respx.post("https://api.getsolari.com/sandboxes/sb-1/exec").mock(
        return_value=httpx.Response(200, json={"exitCode": 0, "stdout": "", "stderr": ""})
    )
    async with SolariClient(api_key="test-key") as client:
        await client.exec("sb-1", "echo", ["ok"], timeout_ms=60_000)
    assert route.calls[0].request.content == b'{"cmd":"echo","args":["ok"],"cwd":null,"timeoutMs":24000}'


@respx.mock
async def test_delete_sandbox_accepts_success_and_not_found() -> None:
    route = respx.delete("https://api.getsolari.com/sandboxes/sb-1").mock(
        side_effect=[httpx.Response(200), httpx.Response(404)]
    )
    async with SolariClient(api_key="test-key") as client:
        assert await client.delete_sandbox("sb-1") is None
        assert await client.delete_sandbox("sb-1") is None
    assert route.call_count == 2
