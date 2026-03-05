/**
 * EventFlux — k6 bulk ingestion load test
 *
 * Configurable via environment variables:
 *   API_BASE_URL        default: http://localhost:8000
 *   EVENTS_PER_REQUEST  default: 100
 *   VUS                 default: 100
 *   DURATION            default: 30s
 *
 * Run (Scenario 1):
 *   k6 run -e API_BASE_URL=http://localhost:8000 \
 *           -e EVENTS_PER_REQUEST=100 \
 *           -e VUS=100 \
 *           load_tests/scenarios/bulk_ingest.js
 *
 * Run (Scenario 2):
 *   k6 run -e API_BASE_URL=http://localhost:8000 \
 *           -e EVENTS_PER_REQUEST=1000 \
 *           -e VUS=50 \
 *           load_tests/scenarios/bulk_ingest.js
 */
import http from "k6/http";
import { check, sleep } from "k6";
import { Rate, Trend } from "k6/metrics";

const API_BASE_URL = __ENV.API_BASE_URL || "http://localhost:8000";
const EVENTS_PER_REQUEST = parseInt(__ENV.EVENTS_PER_REQUEST || "100", 10);
const VUS = parseInt(__ENV.VUS || "100", 10);
const DURATION = __ENV.DURATION || "30s";

const errorRate = new Rate("error_rate");
const ingestTrend = new Trend("ingest_duration_ms", true);

export const options = {
  vus: VUS,
  duration: DURATION,
  thresholds: {
    http_req_failed: ["rate<0.01"],          // <1% error rate
    http_req_duration: ["p(95)<2000"],       // 95th percentile < 2 s
    error_rate: ["rate<0.01"],
  },
};

const EVENT_TYPES = [
  "project_created", "api_call", "user_signup",
  "feature_enabled", "page_viewed", "error_raised",
];
const SOURCES = ["web", "mobile", "api", "cli"];
const PLANS = ["free", "pro", "enterprise"];
const REGIONS = ["us-east", "eu-west", "ap-south"];

function randomElement(arr) {
  return arr[Math.floor(Math.random() * arr.length)];
}

function makeEvent() {
  return {
    event_type: randomElement(EVENT_TYPES),
    actor_id: `user_${Math.floor(Math.random() * 10000).toString().padStart(5, "0")}`,
    source: randomElement(SOURCES),
    timestamp: new Date().toISOString(),
    attributes: {
      plan: randomElement(PLANS),
      region: randomElement(REGIONS),
      latency_ms: Math.floor(Math.random() * 500) + 1,
    },
  };
}

function buildPayload() {
  const events = [];
  for (let i = 0; i < EVENTS_PER_REQUEST; i++) {
    events.push(makeEvent());
  }
  return JSON.stringify({ events });
}

const HEADERS = { "Content-Type": "application/json" };

export default function () {
  const payload = buildPayload();
  const start = Date.now();

  const res = http.post(`${API_BASE_URL}/v1/events/bulk`, payload, {
    headers: HEADERS,
  });

  const durationMs = Date.now() - start;
  ingestTrend.add(durationMs);

  const ok = check(res, {
    "status is 202": (r) => r.status === 202,
    "queued > 0": (r) => {
      try {
        const body = JSON.parse(r.body);
        return body.queued > 0;
      } catch {
        return false;
      }
    },
  });

  errorRate.add(!ok);
  sleep(0.1);
}
