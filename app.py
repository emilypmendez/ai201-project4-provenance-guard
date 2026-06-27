import uuid
from datetime import datetime, timezone

from dotenv import load_dotenv

load_dotenv()

from flask import Flask, request, jsonify
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from pipeline import scorer
from db import audit_log

app = Flask(__name__)

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

audit_log.init_db()


def _now():
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Submission
# ---------------------------------------------------------------------------

@app.route("/submit", methods=["POST"])
@limiter.limit("10 per minute")
def submit():
    body = request.get_json(silent=True) or {}
    content = body.get("content", "").strip()
    creator_id = body.get("creator_id", "anonymous")

    if not content:
        return jsonify({"error": "content is required"}), 400
    if len(content) < 20:
        return jsonify({"error": "content is too short to analyze (minimum 20 characters)"}), 400

    content_id = str(uuid.uuid4())
    result = scorer.run(content)
    timestamp = _now()

    audit_log.log_submission(content_id, creator_id, content, result, timestamp)

    return jsonify({
        "content_id": content_id,
        "attribution": result["attribution"],
        "confidence": result["confidence"],
        "label": result["label"],
        "signals": result["signals"],
        "status": "classified",
        "submitted_at": timestamp,
    }), 200


# ---------------------------------------------------------------------------
# Appeal
# ---------------------------------------------------------------------------

@app.route("/appeal", methods=["POST"])
@limiter.limit("5 per hour")
def appeal():
    body = request.get_json(silent=True) or {}
    content_id = body.get("content_id", "").strip()
    creator_id = body.get("creator_id", "anonymous")
    reasoning = body.get("reasoning", "").strip()

    if not content_id:
        return jsonify({"error": "content_id is required"}), 400
    if not reasoning:
        return jsonify({"error": "reasoning is required"}), 400

    submission = audit_log.get_submission(content_id)
    if not submission:
        return jsonify({"error": "content_id not found"}), 404

    appeal_id = str(uuid.uuid4())
    timestamp = _now()
    audit_log.log_appeal(appeal_id, content_id, creator_id, reasoning, timestamp)

    return jsonify({
        "appeal_id": appeal_id,
        "content_id": content_id,
        "status": "under_review",
        "message": "Your appeal has been received and will be reviewed.",
        "timestamp": timestamp,
    }), 200


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@app.route("/status/<content_id>", methods=["GET"])
def status(content_id):
    submission = audit_log.get_submission(content_id)
    if not submission:
        return jsonify({"error": "content_id not found"}), 404
    return jsonify(submission), 200


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

@app.route("/log", methods=["GET"])
@limiter.limit("30 per minute")
def log():
    try:
        limit = int(request.args.get("limit", 20))
        offset = int(request.args.get("offset", 0))
    except ValueError:
        return jsonify({"error": "limit and offset must be integers"}), 400

    limit = min(limit, 100)
    entries = audit_log.get_log(limit=limit, offset=offset)
    return jsonify({"entries": entries, "count": len(entries)}), 200


# ---------------------------------------------------------------------------
# Analytics dashboard
# ---------------------------------------------------------------------------

@app.route("/analytics", methods=["GET"])
@limiter.limit("30 per minute")
def analytics():
    data = audit_log.get_analytics()
    return jsonify(data), 200


# ---------------------------------------------------------------------------
# Provenance certificate
# ---------------------------------------------------------------------------

@app.route("/certificate/request", methods=["POST"])
@limiter.limit("10 per hour")
def certificate_request():
    body = request.get_json(silent=True) or {}
    content_id = body.get("content_id", "").strip()
    creator_id = body.get("creator_id", "anonymous")
    statement = body.get("statement", "").strip()
    draft_evidence = body.get("draft_evidence", "").strip()

    if not content_id:
        return jsonify({"error": "content_id is required"}), 400
    if not statement:
        return jsonify({"error": "statement is required — describe in your own words that this is your original human-written work"}), 400

    submission = audit_log.get_submission(content_id)
    if not submission:
        return jsonify({"error": "content_id not found"}), 404

    if submission["attribution"] == "ai":
        return jsonify({
            "error": "A provenance certificate cannot be issued for content classified as AI-generated. "
                     "If you believe this is incorrect, please file an appeal first."
        }), 409

    if submission.get("certificate_id"):
        return jsonify({
            "error": "A certificate has already been issued for this content.",
            "certificate_id": submission["certificate_id"],
        }), 409

    certificate_id = str(uuid.uuid4())
    timestamp = _now()
    audit_log.issue_certificate(certificate_id, content_id, creator_id, statement, draft_evidence, timestamp)

    return jsonify({
        "certificate_id": certificate_id,
        "content_id": content_id,
        "creator_id": creator_id,
        "issued_at": timestamp,
        "badge": "✓ Verified Human",
        "message": "Provenance certificate issued. This badge can now be displayed alongside your content.",
    }), 201


@app.route("/certificate/<certificate_id>", methods=["GET"])
def certificate_get(certificate_id):
    cert = audit_log.get_certificate(certificate_id)
    if not cert:
        return jsonify({"error": "certificate not found"}), 404
    cert["badge"] = "✓ Verified Human"
    cert["badge_description"] = (
        "The creator of this content has attested that it is original human-authored work. "
        "This certificate does not guarantee authenticity but records the creator's formal declaration."
    )
    return jsonify(cert), 200


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    app.run(debug=True, port=5000)
