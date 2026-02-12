# Nova Bridge

Standalone Python sidecar that connects your OpenClaw bot to the Nova messaging platform. It bridges the Nova WebSocket API Gateway to your local OpenClaw gateway's `/v1/chat/completions` endpoint — no OpenClaw fork required.

```
Nova user ──WS──▶ API Gateway ──WS──▶ nova-bridge (this sidecar)
                                        │
                                        │  POST /v1/chat/completions
                                        ▼
                                   OpenClaw gateway (localhost:18789)
                                        │
Nova user ◀──WS── API Gateway ◀──WS── nova-bridge
```

## Prerequisites

- Python 3.9+
- A running OpenClaw gateway (`openclaw gateway` on the default port 18789)
- A Nova API key and userId

## Quick Start

### 1. Install

```bash
pip install git+https://github.com/amazon-nova-api/nova-bridge.git
```

### 2. Configure

Create a `config.json` in the directory where you'll run the bridge, or use environment variables:

```bash
export NOVA_API_KEY="your-nova-api-key"
export NOVA_USER_ID="your-user-id"
export OPENCLAW_GATEWAY_TOKEN="your-gateway-token"
```

Or copy the example config and fill in your credentials:

```bash
cp config.example.json config.json
```

```json
{
  "nova_ws_url": "wss://ws.nova-claw.agi.amazon.dev",
  "nova_api_key": "your-nova-api-key",
  "nova_user_id": "your-user-id",
  "openclaw_gateway_url": "http://127.0.0.1:18789",
  "openclaw_gateway_token": "your-gateway-token",
  "dm_policy": "open",
  "allow_from": [],
  "max_history": 20
}
```

### 3. Start OpenClaw gateway (if not already running)

```bash
openclaw gateway --port 18789
```

The gateway token is shown when the gateway starts, or you can find it in `~/.openclaw/openclaw.json` under `gateway.auth.token`.

### 4. Run

```bash
nova-bridge
```

You should see:

```
Nova Bridge starting
  WS endpoint : wss://ws.nova-claw.agi.amazon.dev
  Bot userId  : your-user-id
  OpenClaw    : http://127.0.0.1:18789
  DM policy   : open
Connecting to wss://ws.nova-claw.agi.amazon.dev
WebSocket connected
```

The sidecar will automatically reconnect with exponential backoff if the WebSocket connection drops.

## Configuration Reference

| Setting | Env Variable | Default | Description |
|---|---|---|---|
| `nova_ws_url` | `NOVA_WS_URL` | `wss://ws.nova-claw.agi.amazon.dev` | Nova WebSocket endpoint |
| `nova_api_key` | `NOVA_API_KEY` | *(required)* | Nova API key for WebSocket auth |
| `nova_user_id` | `NOVA_USER_ID` | *(required)* | Your Nova userId (the bot's identity) |
| `nova_device_id` | `NOVA_DEVICE_ID` | *(auto-generated UUID)* | Stable device ID for session correlation |
| `openclaw_gateway_url` | `OPENCLAW_GATEWAY_URL` | `http://127.0.0.1:18789` | Local OpenClaw gateway URL |
| `openclaw_gateway_token` | `OPENCLAW_GATEWAY_TOKEN` | *(empty)* | Gateway auth token |
| `dm_policy` | `DM_POLICY` | `allowlist` | `"open"` to accept all messages, `"allowlist"` to restrict |
| `allow_from` | `ALLOW_FROM` | `[]` | Comma-separated user IDs (for allowlist policy). Use `*` to allow everyone. |
| `max_history` | `MAX_HISTORY` | `20` | Max messages to keep per user for conversation context |

## DM Policy

- **`open`** — Accept messages from any Nova user.
- **`allowlist`** — Only accept messages from user IDs listed in `allow_from`. An empty allowlist blocks everyone. Set `allow_from` to `["*"]` to allow all (equivalent to `open`).

## Running as a systemd Service

A service file template is included for long-running deployments (e.g., on EC2):

```bash
# Create a .env file with your credentials
cat > /home/ec2-user/nova-bridge/.env << 'EOF'
NOVA_API_KEY=your-nova-api-key
NOVA_USER_ID=your-user-id
OPENCLAW_GATEWAY_TOKEN=your-gateway-token
DM_POLICY=open
EOF

# Install and start the service
cp nova-bridge.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now nova-bridge.service

# Check status and logs
systemctl --user status nova-bridge.service
journalctl --user -u nova-bridge.service -f
```

## Testing

The included test script sends a message to the bot via the Nova API Gateway and verifies the response comes back on a test user's WebSocket. Requires AWS credentials with access to the Nova DynamoDB table and API Gateway Management API.

```bash
# Install test dependencies
pip install nova-bridge[test]

# List active WebSocket connections
python test_nova_ws.py --list

# Send a test message (targets the bot by userId)
python test_nova_ws.py --user-id your-user-id -m "Hello, are you there?"

# With a custom timeout
python test_nova_ws.py --user-id your-user-id -m "What is 2+2?" --timeout 30
```

Set `NOVA_API_KEY` in your environment before running the test script.
