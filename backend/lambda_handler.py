"""AWS Lambda entry point: wraps the FastAPI app with Mangum."""
import logging

logger = logging.getLogger("reltio_access.lambda")

try:
    from mangum import Mangum
    from app.main import app
    handler = Mangum(app)
except Exception:
    logger.exception("Failed to initialize the Lambda handler")

    def handler(event, context):
        return {
            "statusCode": 500,
            "body": '{"detail":"Internal server error. Check CloudWatch logs."}',
            "headers": {"Content-Type": "application/json"},
        }
