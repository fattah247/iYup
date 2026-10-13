#!/bin/bash
# Script to start ping-agent and api-gateway services for reliability testing

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
PING_AGENT_DIR="$PROJECT_ROOT/services/ping-agent"
API_GATEWAY_DIR="$PROJECT_ROOT/services/api-gateway"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log() {
    echo -e "${GREEN}[$(date +'%Y-%m-%d %H:%M:%S')]${NC} $1"
}

error() {
    echo -e "${RED}[ERROR]${NC} $1" >&2
}

warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

# Check if services are already running
check_port() {
    local port=$1
    if lsof -Pi :$port -sTCP:LISTEN -t >/dev/null 2>&1; then
        return 0
    else
        return 1
    fi
}

# Kill process on port
kill_port() {
    local port=$1
    local pid=$(lsof -ti:$port 2>/dev/null || true)
    if [ -n "$pid" ]; then
        log "Killing process on port $port (PID: $pid)"
        kill $pid 2>/dev/null || true
        sleep 1
    fi
}

# Cleanup function
cleanup() {
    log "Cleaning up..."
    kill_port 18080
    kill_port 18081
    if [ -n "$PING_AGENT_PID" ]; then
        kill $PING_AGENT_PID 2>/dev/null || true
    fi
    if [ -n "$API_GATEWAY_PID" ]; then
        kill $API_GATEWAY_PID 2>/dev/null || true
    fi
}

trap cleanup EXIT INT TERM

# Check prerequisites
if ! command -v go &> /dev/null; then
    error "Go is not installed. Please install Go to run ping-agent."
    exit 1
fi

if ! command -v python3 &> /dev/null; then
    error "Python 3 is not installed. Please install Python 3 to run api-gateway."
    exit 1
fi

# Check if ports are already in use
if check_port 18080; then
    warn "Port 18080 is already in use. Attempting to free it..."
    kill_port 18080
    sleep 2
fi

if check_port 18081; then
    warn "Port 18081 is already in use. Attempting to free it..."
    kill_port 18081
    sleep 2
fi

# Start ping-agent
log "Starting ping-agent on port 18080..."
cd "$PING_AGENT_DIR"
export PING_TARGET_URLS="https://google.com,https://github.com"
export PING_INTERVAL_SECONDS=30
go run . &
PING_AGENT_PID=$!

# Wait for ping-agent to be ready
log "Waiting for ping-agent to be ready..."
for i in {1..30}; do
    if curl -s http://localhost:18080/healthz >/dev/null 2>&1; then
        log "ping-agent is ready!"
        break
    fi
    if [ $i -eq 30 ]; then
        error "ping-agent failed to start after 30 seconds"
        exit 1
    fi
    sleep 1
done

# Start api-gateway
log "Starting api-gateway on port 18081..."
cd "$API_GATEWAY_DIR"

# Create virtual environment if it doesn't exist
if [ ! -d ".venv" ]; then
    log "Creating virtual environment..."
    python3 -m venv .venv
fi

# Activate virtual environment and install dependencies
source .venv/bin/activate
if [ ! -f ".venv/.deps_installed" ]; then
    log "Installing dependencies..."
    pip install -q -r requirements.txt
    touch .venv/.deps_installed
fi

# Set environment variables
export PING_AGENT_METRICS_URL="http://localhost:18080/metrics"
export PING_TARGET_URLS="https://google.com,https://github.com"

# Start api-gateway
uvicorn main:app --host 0.0.0.0 --port 18081 --log-level warning &
API_GATEWAY_PID=$!

# Wait for api-gateway to be ready
log "Waiting for api-gateway to be ready..."
for i in {1..30}; do
    if curl -s http://localhost:18081/healthz >/dev/null 2>&1; then
        log "api-gateway is ready!"
        break
    fi
    if [ $i -eq 30 ]; then
        error "api-gateway failed to start after 30 seconds"
        exit 1
    fi
    sleep 1
done

log ""
log "=========================================="
log "Services are running!"
log "=========================================="
log "ping-agent:  http://localhost:18080"
log "api-gateway: http://localhost:18081"
log ""
log "Test endpoints:"
log "  curl http://localhost:18080/healthz"
log "  curl http://localhost:18081/healthz"
log "  curl http://localhost:18081/uptime-summary"
log "  curl http://localhost:18081/targets"
log ""
log "Press Ctrl+C to stop all services"
log "=========================================="
log ""

# Wait for user interrupt
wait
