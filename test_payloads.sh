#!/usr/bin/env bash
# Demo payloads for testing the Argos /alert-webhook endpoint locally.
# Usage: bash test_payloads.sh [scenario]
# Examples:
#   bash test_payloads.sh ecs_cpu        (default)
#   bash test_payloads.sh ecs_memory
#   bash test_payloads.sh rds_connections
#   bash test_payloads.sh lambda_errors
#   bash test_payloads.sh alb_5xx
#   bash test_payloads.sh ec2_cpu

BASE_URL="${ARGOS_URL:-http://localhost:8080}"
SCENARIO="${1:-ecs_cpu}"

# ── Payloads ──────────────────────────────────────────────────────────────────

payload_ecs_cpu() {
cat <<'EOF'
{
  "version": "0",
  "id": "aabbccdd-1234-5678-abcd-111122223333",
  "source": "aws.cloudwatch",
  "detail-type": "CloudWatch Alarm State Change",
  "account": "686255973607",
  "time": "2024-01-15T10:30:00Z",
  "region": "ap-south-1",
  "detail": {
    "alarmName": "p1-ecs-payment-service-cpu-high",
    "alarmArn": "arn:aws:cloudwatch:ap-south-1:686255973607:alarm:p1-ecs-payment-service-cpu-high",
    "state": {
      "value": "ALARM",
      "reason": "Threshold Crossed: 3 datapoints [92.3, 89.1, 91.7] were greater than the threshold (80.0).",
      "reasonData": "{\"version\":\"1.0\",\"queryDate\":\"2024-01-15T10:30:00.000+0000\",\"statistic\":\"Average\",\"period\":300,\"recentDatapoints\":[92.3,89.1,91.7],\"threshold\":80.0}",
      "timestamp": "2024-01-15T10:30:00.000+0000"
    },
    "previousState": {
      "value": "OK",
      "reason": "Threshold Crossed: 1 datapoint was not greater than the threshold.",
      "timestamp": "2024-01-15T09:00:00.000+0000"
    },
    "configuration": {
      "description": "ECS CPUUtilization > 80% for 3 consecutive 5-minute periods",
      "metrics": [
        {
          "id": "m1",
          "metricStat": {
            "metric": {
              "namespace": "AWS/ECS",
              "name": "CPUUtilization",
              "dimensions": {
                "ClusterName": "prod-cluster",
                "ServiceName": "payment-service"
              }
            },
            "period": 300,
            "stat": "Average",
            "unit": "Percent"
          },
          "returnData": true
        }
      ]
    }
  }
}
EOF
}

payload_ecs_memory() {
cat <<'EOF'
{
  "version": "0",
  "id": "bbccddee-2345-6789-bcde-222233334444",
  "source": "aws.cloudwatch",
  "detail-type": "CloudWatch Alarm State Change",
  "account": "123456789012",
  "time": "2024-01-15T11:00:00Z",
  "region": "ap-south-1",
  "detail": {
    "alarmName": "p2-ecs-order-service-memory-high",
    "alarmArn": "arn:aws:cloudwatch:ap-south-1:123456789012:alarm:p2-ecs-order-service-memory-high",
    "state": {
      "value": "ALARM",
      "reason": "Threshold Crossed: 2 datapoints [88.0, 91.2] were greater than the threshold (85.0).",
      "timestamp": "2024-01-15T11:00:00.000+0000"
    },
    "previousState": {
      "value": "OK",
      "timestamp": "2024-01-15T10:00:00.000+0000"
    },
    "configuration": {
      "description": "ECS MemoryUtilization > 85%",
      "metrics": [
        {
          "id": "m1",
          "metricStat": {
            "metric": {
              "namespace": "AWS/ECS",
              "name": "MemoryUtilization",
              "dimensions": {
                "ClusterName": "prod-cluster",
                "ServiceName": "order-service"
              }
            },
            "period": 300,
            "stat": "Average"
          },
          "returnData": true
        }
      ]
    }
  }
}
EOF
}

payload_rds_connections() {
cat <<'EOF'
{
  "version": "0",
  "id": "ccddee11-3456-7890-cdef-333344445555",
  "source": "aws.cloudwatch",
  "detail-type": "CloudWatch Alarm State Change",
  "account": "123456789012",
  "time": "2024-01-15T12:00:00Z",
  "region": "ap-south-1",
  "detail": {
    "alarmName": "p2-rds-main-db-connections-high",
    "alarmArn": "arn:aws:cloudwatch:ap-south-1:123456789012:alarm:p2-rds-main-db-connections-high",
    "state": {
      "value": "ALARM",
      "reason": "Threshold Crossed: 1 datapoint [498.0] was greater than the threshold (400.0).",
      "timestamp": "2024-01-15T12:00:00.000+0000"
    },
    "previousState": {
      "value": "OK",
      "timestamp": "2024-01-15T11:00:00.000+0000"
    },
    "configuration": {
      "description": "RDS DatabaseConnections > 400",
      "metrics": [
        {
          "id": "m1",
          "metricStat": {
            "metric": {
              "namespace": "AWS/RDS",
              "name": "DatabaseConnections",
              "dimensions": {
                "DBInstanceIdentifier": "prod-main-db"
              }
            },
            "period": 60,
            "stat": "Average"
          },
          "returnData": true
        }
      ]
    }
  }
}
EOF
}

payload_lambda_errors() {
cat <<'EOF'
{
  "version": "0",
  "id": "ddeeff22-4567-8901-def0-444455556666",
  "source": "aws.cloudwatch",
  "detail-type": "CloudWatch Alarm State Change",
  "account": "123456789012",
  "time": "2024-01-15T13:00:00Z",
  "region": "ap-south-1",
  "detail": {
    "alarmName": "p1-lambda-order-processor-errors-spike",
    "alarmArn": "arn:aws:cloudwatch:ap-south-1:123456789012:alarm:p1-lambda-order-processor-errors-spike",
    "state": {
      "value": "ALARM",
      "reason": "Threshold Crossed: 1 datapoint [47.0] was greater than the threshold (5.0).",
      "timestamp": "2024-01-15T13:00:00.000+0000"
    },
    "previousState": {
      "value": "OK",
      "timestamp": "2024-01-15T12:30:00.000+0000"
    },
    "configuration": {
      "description": "Lambda Errors > 5 in 5 minutes",
      "metrics": [
        {
          "id": "m1",
          "metricStat": {
            "metric": {
              "namespace": "AWS/Lambda",
              "name": "Errors",
              "dimensions": {
                "FunctionName": "order-processor"
              }
            },
            "period": 300,
            "stat": "Sum"
          },
          "returnData": true
        }
      ]
    }
  }
}
EOF
}

payload_alb_5xx() {
cat <<'EOF'
{
  "version": "0",
  "id": "eeff0033-5678-9012-ef01-555566667777",
  "source": "aws.cloudwatch",
  "detail-type": "CloudWatch Alarm State Change",
  "account": "123456789012",
  "time": "2024-01-15T14:00:00Z",
  "region": "ap-south-1",
  "detail": {
    "alarmName": "p1-alb-api-gateway-5xx-rate-high",
    "alarmArn": "arn:aws:cloudwatch:ap-south-1:123456789012:alarm:p1-alb-api-gateway-5xx-rate-high",
    "state": {
      "value": "ALARM",
      "reason": "Threshold Crossed: 1 datapoint [12.4] was greater than the threshold (5.0).",
      "timestamp": "2024-01-15T14:00:00.000+0000"
    },
    "previousState": {
      "value": "OK",
      "timestamp": "2024-01-15T13:30:00.000+0000"
    },
    "configuration": {
      "description": "ALB HTTPCode_Target_5XX_Count > 5% for 5 minutes",
      "metrics": [
        {
          "id": "m1",
          "metricStat": {
            "metric": {
              "namespace": "AWS/ApplicationELB",
              "name": "HTTPCode_Target_5XX_Count",
              "dimensions": {
                "LoadBalancer": "app/prod-api-alb/1234567890abcdef",
                "TargetGroup": "targetgroup/prod-api-tg/abcdef1234567890"
              }
            },
            "period": 300,
            "stat": "Sum"
          },
          "returnData": true
        }
      ]
    }
  }
}
EOF
}

payload_ec2_cpu() {
cat <<'EOF'
{
  "version": "0",
  "id": "ff001144-6789-0123-f012-666677778888",
  "source": "aws.cloudwatch",
  "detail-type": "CloudWatch Alarm State Change",
  "account": "123456789012",
  "time": "2024-01-15T15:00:00Z",
  "region": "ap-south-1",
  "detail": {
    "alarmName": "p3-ec2-web-server-03-cpu-high",
    "alarmArn": "arn:aws:cloudwatch:ap-south-1:123456789012:alarm:p3-ec2-web-server-05-cpu-high",
    "state": {
      "value": "ALARM",
      "reason": "Threshold Crossed: 1 datapoint [95.0] was greater than the threshold (90.0).",
      "timestamp": "2024-01-15T15:00:00.000+0000"
    },
    "previousState": {
      "value": "OK",
      "timestamp": "2024-01-15T14:00:00.000+0000"
    },
    "configuration": {
      "description": "EC2 CPUUtilization > 90%",
      "metrics": [
        {
          "id": "m1",
          "metricStat": {
            "metric": {
              "namespace": "AWS/EC2",
              "name": "CPUUtilization",
              "dimensions": {
                "InstanceId": "i-0abc123def456789a"
              }
            },
            "period": 300,
            "stat": "Average"
          },
          "returnData": true
        }
      ]
    }
  }
}
EOF
}

# ── Send ──────────────────────────────────────────────────────────────────────

echo "Sending scenario: ${SCENARIO}"
echo "Target: ${BASE_URL}/alert-webhook"
echo "─────────────────────────────────────────"

case "$SCENARIO" in
  ecs_cpu)         PAYLOAD=$(payload_ecs_cpu) ;;
  ecs_memory)      PAYLOAD=$(payload_ecs_memory) ;;
  rds_connections) PAYLOAD=$(payload_rds_connections) ;;
  lambda_errors)   PAYLOAD=$(payload_lambda_errors) ;;
  alb_5xx)         PAYLOAD=$(payload_alb_5xx) ;;
  ec2_cpu)         PAYLOAD=$(payload_ec2_cpu) ;;
  *)
    echo "Unknown scenario: ${SCENARIO}"
    echo "Available: ecs_cpu, ecs_memory, rds_connections, lambda_errors, alb_5xx, ec2_cpu"
    exit 1
    ;;
esac

echo "$PAYLOAD" | python3 -m json.tool --no-ensure-ascii 2>/dev/null || true
echo ""
echo "─────────────────────────────────────────"

curl -s -X POST "${BASE_URL}/alert-webhook" \
  -H "Content-Type: application/json" \
  -d "$PAYLOAD" | python3 -m json.tool
