import os
import json
import uuid
import boto3
import redis
from flask import Flask, request, jsonify

app = Flask(__name__)

# Redis 연결
r = redis.Redis(host=os.getenv("REDIS_HOST", "redis"), port=6379, decode_responses=True)

# S3 클라이언트 설정 (EC2 IAM 역할 자동 인식)
S3_BUCKET = os.getenv("S3_BUCKET_NAME", "kbo-ticket-2224024")
AWS_REGION = os.getenv("AWS_REGION", "ap-southeast-2")
s3 = boto3.client("s3", region_name=AWS_REGION)

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


# 0. 헬스 체크
@app.route("/api/health", methods=["GET"])
def health_check():
    try:
        r.ping()
        return jsonify({"status": "ok", "redis": "connected", "s3_bucket": S3_BUCKET}), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


# 1. 경기/좌석 구역 조회, 등록, 삭제 (S3 연동)
@app.route("/api/sections", methods=["GET"])
def get_sections():
    section_keys = sorted(r.keys("section:*"))
    sections = []
    for key in section_keys:
        data = json.loads(r.get(key))
        seats_status = {}
        for seat in data["seats"]:
            lock_key = f"ticket:{data['id']}:{seat}"
            booked_info = r.get(lock_key)
            seats_status[seat] = json.loads(booked_info) if booked_info else None
        data["seats_status"] = seats_status
        sections.append(data)
    return jsonify(sections), 200


@app.route("/api/sections", methods=["POST"])
def create_section():
    title = request.form.get("title", "").strip()
    seats_raw = request.form.get("seats", "").strip()
    file = request.files.get("image")

    # 경계 및 예외 검증
    if not title or not seats_raw:
        return jsonify({"error": "경기/구역명과 예매 가능 좌석 번호를 모두 입력하세요."}), 400
    if len(title) > 50:
        return jsonify({"error": "경기/구역명은 50자 이하로 입력하세요."}), 400
    if not file or file.filename == "":
        return jsonify({"error": "좌석 시야 이미지 파일을 첨부해야 합니다."}), 400
    if not allowed_file(file.filename):
        return jsonify({"error": "허용되지 않는 파일 형식입니다. (png, jpg, jpeg, gif, webp만 가능)"}), 400

    # 중복 좌석 제거 및 순서 유지
    seats = list(dict.fromkeys([s.strip() for s in seats_raw.split(",") if s.strip()]))
    if not seats:
        return jsonify({"error": "유효한 좌석 번호를 1개 이상 입력하세요."}), 400

    # 한글 파일명 안전 처리: 원본 파일명에서 확장자 직접 추출
    ext = file.filename.rsplit(".", 1)[1].lower()
    s3_key = f"views/{uuid.uuid4().hex}.{ext}"
    try:
        s3.upload_fileobj(
            file,
            S3_BUCKET,
            s3_key,
            ExtraArgs={"ContentType": file.content_type or "image/jpeg"},
        )
        image_url = f"https://{S3_BUCKET}.s3.{AWS_REGION}.amazonaws.com/{s3_key}"
    except Exception as e:
        return jsonify({"error": f"S3 업로드 실패: {str(e)}"}), 500

    section_id = uuid.uuid4().hex[:8]
    section_data = {
        "id": section_id,
        "title": title,
        "seats": seats,
        "image_url": image_url,
        "s3_key": s3_key,
    }
    r.set(f"section:{section_id}", json.dumps(section_data))
    return jsonify({"message": "경기 및 좌석 구역 등록 완료!", "section": section_data}), 201


@app.route("/api/sections/<section_id>", methods=["DELETE"])
def delete_section(section_id):
    section_raw = r.get(f"section:{section_id}")
    if not section_raw:
        return jsonify({"error": "존재하지 않는 경기/구역입니다."}), 404

    section_data = json.loads(section_raw)
    # S3 객체 삭제
    try:
        if section_data.get("s3_key"):
            s3.delete_object(Bucket=S3_BUCKET, Key=section_data["s3_key"])
    except Exception:
        pass

    # Redis 구역 및 해당 구역 예매 데이터 삭제
    r.delete(f"section:{section_id}")
    for seat in section_data.get("seats", []):
        r.delete(f"ticket:{section_id}:{seat}")

    return jsonify({"message": "구역 및 S3 이미지가 삭제되었습니다."}), 200


# 2. 실시간 선착순 좌석 예매 (Redis SET NX 동시성 제어)
@app.route("/api/reserve", methods=["POST"])
def reserve_seat():
    data = request.get_json() or {}
    section_id = data.get("section_id", "").strip()
    seat = data.get("seat", "").strip()
    fan_id = data.get("fan_id", "").strip()
    fan_name = data.get("fan_name", "").strip()

    if not all([section_id, seat, fan_id, fan_name]):
        return jsonify({"error": "팬 ID(연락처), 이름, 구역, 좌석을 모두 입력하세요."}), 400
    if len(fan_id) > 30 or len(fan_name) > 20:
        return jsonify({"error": "팬 ID는 30자, 이름은 20자 이하로 입력하세요."}), 400

    section_raw = r.get(f"section:{section_id}")
    if not section_raw:
        return jsonify({"error": "존재하지 않는 경기/구역입니다."}), 404

    section_data = json.loads(section_raw)
    if seat not in section_data["seats"]:
        return jsonify({"error": "해당 구역에 존재하지 않는 좌석 번호입니다."}), 400

    lock_key = f"ticket:{section_id}:{seat}"
    ticket_payload = json.dumps({
        "section_id": section_id,
        "section_title": section_data["title"],
        "seat": seat,
        "fan_id": fan_id,
        "fan_name": fan_name,
    })

    is_success = r.set(lock_key, ticket_payload, nx=True)
    if not is_success:
        return jsonify({"error": "이미 선착순 매진된 좌석입니다."}), 409

    return jsonify({"message": f"[{section_data['title']} / {seat}석] 선착순 예매가 확정되었습니다!"}), 201


# 3. 내 티켓 조회 및 예매 취소
@app.route("/api/my-tickets", methods=["GET"])
def get_my_tickets():
    fan_id = request.args.get("fan_id", "").strip()
    if not fan_id:
        return jsonify({"error": "조회할 팬 ID(연락처)를 입력하세요."}), 400

    results = []
    for key in r.keys("ticket:*"):
        info = json.loads(r.get(key))
        if info["fan_id"] == fan_id:
            results.append(info)

    if not results:
        return jsonify({"error": "해당 ID로 예매된 티켓 내역이 없습니다."}), 404

    return jsonify(results), 200


@app.route("/api/cancel", methods=["POST"])
def cancel_ticket():
    data = request.get_json() or {}
    section_id = data.get("section_id", "").strip()
    seat = data.get("seat", "").strip()
    fan_id = data.get("fan_id", "").strip()

    if not all([section_id, seat, fan_id]):
        return jsonify({"error": "취소할 티켓 정보와 팬 ID를 입력하세요."}), 400

    lock_key = f"ticket:{section_id}:{seat}"
    booked_raw = r.get(lock_key)
    if not booked_raw:
        return jsonify({"error": "이미 취소되었거나 존재하지 않는 예매 내역입니다."}), 404

    booked_info = json.loads(booked_raw)
    if booked_info["fan_id"] != fan_id:
        return jsonify({"error": "본인의 팬 ID와 일치하지 않아 취소할 수 없습니다."}), 403

    r.delete(lock_key)
    return jsonify({"message": "티켓 예매가 정상적으로 취소되었습니다."}), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)