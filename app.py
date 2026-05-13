from flask import Flask, jsonify, request, send_file
import os, json, tempfile, zipfile
from datetime import datetime, timedelta
from urllib.parse import urlparse
import boto3, rasterio, requests
from rasterio.windows import from_bounds
from rasterio.io import MemoryFile

# -------------------------
# AWS REGION 고정 (리전 mismatch 방지)
# -------------------------
os.environ["AWS_DEFAULT_REGION"] = "ap-northeast-2"
os.environ["AWS_REGION"] = "ap-northeast-2"

# boto3 region + endpoint 강제 고정
s3 = boto3.client(
    "s3",
    region_name="ap-northeast-2"
)

# -------------------------
# CONFIG
# -------------------------
BUCKET = "water-resources"
ITEM_DIR = "stac"
EXPIRES_IN = 3600   # presigned URL 만료 (초)

app = Flask(__name__)

# -------------------------
# UTIL FUNCTIONS
# -------------------------

def find_item_key(date):
    """date=YYYYMMDD 로 STAC item JSON 경로를 찾는다."""
    candidates = [
        f"{ITEM_DIR}/{date}/{date}.json",
        f"{ITEM_DIR}/{date}.json"
    ]
    for key in candidates:
        try:
            s3.head_object(Bucket=BUCKET, Key=key)
            return key
        except:
            pass
    return None


def get_href(date):
    """STAC item JSON에서 assets.data.href 추출"""
    key = find_item_key(date)
    if not key:
        return None
    obj = s3.get_object(Bucket=BUCKET, Key=key)
    item = json.loads(obj["Body"].read())
    return item["assets"]["data"]["href"]


def href_to_presigned(href):
    """s3://bucket/key → presigned URL"""
    u = urlparse(href)
    if u.scheme in ("http", "https"):
        return href
    bucket, key = u.netloc, u.path.lstrip("/")
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=EXPIRES_IN
    )


def stream_dataset(url):
    """presigned URL → rasterio dataset"""
    r = requests.get(url, stream=True)
    r.raise_for_status()
    mem = MemoryFile(r.raw)
    return mem, mem.open()


# -------------------------
# WGS84 전용 클리핑 (QGIS 하얀 화면 문제 해결)
# -------------------------
def clip_raster(ds, bbox):
    """
    bbox = [minLon, minLat, maxLon, maxLat] (WGS84)
    ds.crs = EPSG:4326
    """
    minx, miny, maxx, maxy = bbox

    # 1) 픽셀 window 계산
    window = from_bounds(minx, miny, maxx, maxy, ds.transform)

    # 2) 데이터 읽기
    data = ds.read(window=window)

    # 3) 새 transform 계산
    new_transform = ds.window_transform(window)

    # 4) nodata 값도 같이 가져감 (없으면 -9999라고 가정)
    nodata = ds.nodata
    if nodata is None:
        # 원본에 명시 nodata가 없는데 실제로 -9999 쓰고 있다면 이렇게 고정
        nodata = -9999

    return data, new_transform, ds.crs, nodata


def save_tif(data, transform, crs, name, nodata=None):
    path = os.path.join(tempfile.gettempdir(), name)

    # nodata 기본값: -9999 (Q에서 이 값을 NoData로 취급하게 하기 위함)
    if nodata is None:
        nodata = -9999

    with rasterio.open(
        path,
        "w",
        driver="GTiff",
        height=data.shape[1],
        width=data.shape[2],
        count=data.shape[0],
        dtype=data.dtype,
        crs=crs,
        transform=transform,
        nodata=nodata          # ★ nodata 명시
    ) as dst:
        dst.write(data)

    return path


# -------------------------
# ENDPOINTS
# -------------------------

# ✔ ① 단일 날짜 presigned URL 조회
@app.get("/href")
def api_href():
    date = request.args.get("date")
    if not date:
        return jsonify({"error": "date=YYYYMMDD required"}), 400

    href = get_href(date)
    if not href:
        return jsonify({"error": f"STAC item for {date} not found"}), 404

    return jsonify({
        "date": date,
        "url": href_to_presigned(href)
    })


# ✔ ② 여러 날짜 원본 tif ZIP 제공 (range)
@app.get("/range")
def api_range_zip():
    start = request.args.get("start")
    end   = request.args.get("end")

    if not (start and end):
        return jsonify({"error": "start,end=YYYYMMDD required"}), 400

    try:
        sdt = datetime.strptime(start, "%Y%m%d")
        edt = datetime.strptime(end, "%Y%m%d")
    except:
        return jsonify({"error": "invalid date"}), 400

    zip_path = os.path.join(tempfile.gettempdir(), f"original_{start}_{end}.zip")

    with zipfile.ZipFile(zip_path, "w") as zipf:
        while sdt <= edt:
            d = sdt.strftime("%Y%m%d")

            href = get_href(d)
            if href:
                url = href_to_presigned(href)

                r = requests.get(url, stream=True)
                if r.status_code == 200:
                    tif_path = os.path.join(tempfile.gettempdir(), f"{d}.tif")
                    with open(tif_path, "wb") as f:
                        for chunk in r.iter_content(chunk_size=8192):
                            f.write(chunk)
                    zipf.write(tif_path, arcname=f"{d}.tif")

            sdt += timedelta(days=1)

    return send_file(zip_path, mimetype="application/zip", as_attachment=True,
                     download_name=f"original_{start}_{end}.zip")


# ✔ ③ bbox + 날짜 범위 클리핑 ZIP
@app.get("/clip")
def api_clip():
    start = request.args.get("start")
    end = request.args.get("end", start)
    bbox_raw = request.args.get("bbox")

    if not (start and bbox_raw):
        return jsonify({"error": "start & bbox=minx,miny,maxx,maxy required"}), 400

    bbox = [float(x) for x in bbox_raw.split(",")]

    sdt = datetime.strptime(start, "%Y%m%d")
    edt = datetime.strptime(end, "%Y%m%d")

    zip_path = os.path.join(tempfile.gettempdir(), f"clip_{start}_{end}.zip")

    with zipfile.ZipFile(zip_path, "w") as zipf:

        while sdt <= edt:
            d = sdt.strftime("%Y%m%d")
            href = get_href(d)

            if href:
                url = href_to_presigned(href)
                mem, ds = stream_dataset(url)

                with ds:
                    # 🔥 nodata까지 받아온다
                    data, tf, crs, nodata = clip_raster(ds, bbox)
                    tif_path = save_tif(data, tf, crs, f"clip_{d}.tif", nodata=nodata)
                    zipf.write(tif_path, arcname=f"clip_{d}.tif")

            sdt += timedelta(days=1)

    return send_file(zip_path, as_attachment=True, mimetype="application/zip")


# -------------------------
# MAIN
# -------------------------
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)


