# -*- coding: utf-8 -*-
"""
IO 密集型基准测试：多线程分块并发下载大文件，测试不同线程数下的下载速度。

【功能】
  - 使用 HTTP Range 请求将大文件分块，多线程并发下载各块后按序写入本地。
  - 测试 1～50 个线程下的总下载速度（MB/s），并绘制「线程数 vs 下载速度」曲线图。
  - 支持将文件下载到指定路径（如 F 盘）：在 SAVE_DIR 或 --output-dir 中填写，例如 F:/下载测试。

【使用方法】
  1. 在下方配置区填写 DOWNLOAD_URL（支持 Range 的大文件 URL）和 SAVE_DIR（本地下载目录，如 F:/下载测试）。
  2. 运行：python io_download_benchmark.py
  3. 或命令行指定：python io_download_benchmark.py --url <URL> --output-dir "F:/下载测试"
  4. 可选参数：--thread-counts 1,2,5,10、--keep / --no-keep、--no-plot

【依赖】
  - requests（推荐）：pip install requests
  - matplotlib（绘图）：pip install matplotlib
  - 未安装 requests 时使用标准库 urllib
"""
import os
import sys
import re
import json
import time
import logging
from urllib.parse import urljoin, urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger(__name__)

# ===================== 配置区（先不填充，运行前请填写） =====================
# 默认：testfile.org 的 1GB 文件页面；脚本会爬取该页并筛选出 1GB 对应的下载链接再下载
# 若该站返回 403，脚本会自动尝试下方备用直链
DOWNLOAD_URL = "https://testfile.org/file-1GB"

# 备用直链：当默认 URL 无法访问（如 403）时自动使用；需支持 Range 请求
# 示例：Tele2 测速文件（通常不反爬），可改为其他支持 Range 的直链
FALLBACK_DIRECT_URL = "http://speedtest.tele2.net/10MB.zip"

# 爬取页面时只处理与此大小标识匹配的下载链接（如 "1GB" 表示只使用 1GB 文件的链接）
DOWNLOAD_SIZE_FILTER = "1GB"

# 待填写：本地下载目录，下载后的文件将保存在此目录下。
# 示例：F 盘指定路径请使用 r"F:\下载测试" 或 "F:/下载测试"（避免反斜杠转义问题）。
SAVE_DIR = "F:/下载测试"

# 默认保存文件名（当无法从 URL 解析出文件名时使用）
DEFAULT_FILENAME = "downloaded_file.bin"

# 测试的线程数列表（1～50，可按需增减）
THREAD_COUNTS = [10, 20, 30, 40, 50]

# 单块下载失败时的重试次数
CHUNK_RETRY_TIMES = 3

# 超时控制（秒）
CONNECT_TIMEOUT = 10   # 建立连接超时
READ_TIMEOUT = 300     # 读取响应超时（大块下载时适当放大）
REQUEST_TIMEOUT = 60   # 用于 HEAD/页面等短请求的总超时；分块下载使用 (CONNECT_TIMEOUT, READ_TIMEOUT)

# 连接池控制（仅在使用 requests 且分块下载时生效）
POOL_CONNECTIONS = 10   # 每个主机保留的连接数
POOL_MAXSIZE = 50       # 连接池最大连接数（建议 >= 线程数，避免排队）

# 浏览器风格请求头，降低被反爬（403）的概率；所有 HEAD/GET/Range 请求均会带上
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Referer": "https://testfile.org/",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


# ===================== 运行环境与使用方法 =====================
def _print_runtime_env():
    """打印当前运行环境（HTTP 库、绘图库），便于排查依赖问题。"""
    try:
        import matplotlib
        HAS_MATPLOTLIB = True
    except ImportError:
        HAS_MATPLOTLIB = False
    print("\n--- 运行环境 ---")
    print("当前 Python:", sys.executable)
    print("requests: ", "已加载" if HAS_REQUESTS else "未加载（将使用 urllib）")
    print("matplotlib:", "已加载" if HAS_MATPLOTLIB else "未加载（将跳过绘图）")
    print("---\n")


def print_usage():
    """打印使用方法（简要）。"""
    print("【使用方法】")
    print("  1. 配置 DOWNLOAD_URL 与 SAVE_DIR（或使用 --url、--output-dir）")
    print("  2. 运行: python io_download_benchmark.py")
    print("  3. 下载文件与曲线图将保存到 SAVE_DIR（可指定 F 盘路径，如 F:/下载测试）")
    print()


# ===================== 优缺点说明 =====================
PROS_CONS = """
【多线程分块下载 优缺点】
  优点：充分利用多线程并发，将大文件按 Range 分块并行下载，提高总吞吐；瓶颈在网络时可显著提升速度；
       可指定本地保存路径（如 F 盘），便于管理磁盘空间。
  缺点：依赖服务器支持 HTTP Range；块过多时连接数、调度开销增加；需按块顺序写入，实现略复杂。
"""


def print_pros_cons():
    """打印多线程分块下载的优缺点说明。"""
    print("\n=== 优缺点说明 ===")
    print(PROS_CONS.strip())


# ===================== HTTP 客户端（优先 requests，否则 urllib） =====================
try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    requests = None
    HAS_REQUESTS = False
    logger.warning("未安装 requests，将使用标准库 urllib。建议: pip install requests")

if HAS_REQUESTS:
    def _head_content_length(url):
        """使用 requests 发送 HEAD 请求，返回 Content-Length；失败返回 None。"""
        try:
            r = requests.head(
                url, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True
            )
            r.raise_for_status()
            cl = r.headers.get("Content-Length")
            return int(cl) if cl else None
        except Exception as e:
            logger.warning("HEAD 请求失败: %s", e)
            return None

    def _get_content_length(url):
        """HEAD 失败时回退：使用 GET 请求（stream=True 仅读头不读 body），返回 Content-Length。"""
        try:
            r = requests.get(
                url, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT,
                allow_redirects=True, stream=True
            )
            r.raise_for_status()
            cl = r.headers.get("Content-Length")
            r.close()
            return int(cl) if cl else None
        except Exception as e:
            logger.warning("GET 回退获取 Content-Length 失败: %s", e)
            return None

    def _get_range(url, start, end):
        """使用 requests 请求 Range 字节段，返回 bytes；失败抛出异常。分块下载使用连接/读取分离超时。"""
        headers = {**DEFAULT_HEADERS, "Range": f"bytes={start}-{end}"}
        r = requests.get(
            url, headers=headers,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            allow_redirects=True
        )
        r.raise_for_status()
        return r.content

    def _get_range_via_session(session, url, start, end):
        """使用 Session 请求 Range 字节段（复用连接池），返回 bytes；失败抛出异常。"""
        headers = {**DEFAULT_HEADERS, "Range": f"bytes={start}-{end}"}
        r = session.get(
            url, headers=headers,
            timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
            allow_redirects=True
        )
        r.raise_for_status()
        return r.content
else:
    import urllib.request
    import urllib.error

    def _head_content_length(url):
        """使用 urllib 发送 HEAD 请求，返回 Content-Length；失败返回 None。"""
        try:
            req = urllib.request.Request(url, method="HEAD", headers=DEFAULT_HEADERS)
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                cl = resp.headers.get("Content-Length")
                return int(cl) if cl else None
        except Exception as e:
            logger.warning("HEAD 请求失败: %s", e)
            return None

    def _get_content_length(url):
        """HEAD 失败时回退：使用 GET 请求，仅读响应头取 Content-Length 后关闭，不读 body。"""
        try:
            req = urllib.request.Request(url, headers=DEFAULT_HEADERS)
            resp = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
            cl = resp.headers.get("Content-Length")
            resp.close()
            return int(cl) if cl else None
        except Exception as e:
            logger.warning("GET 回退获取 Content-Length 失败: %s", e)
            return None

    def _get_range(url, start, end):
        """使用 urllib 请求 Range 字节段，返回 bytes；失败抛出异常。使用 READ_TIMEOUT 作为总超时。"""
        headers = {**DEFAULT_HEADERS, "Range": f"bytes={start}-{end}"}
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=READ_TIMEOUT) as resp:
            return resp.read()


def _fetch_page_html(url):
    """
    拉取页面 HTML 文本（用于从页面中解析下载链接）。使用浏览器风格请求头降低 403。
    :param url: 页面 URL
    :return: HTML 字符串，失败返回 None
    """
    try:
        if HAS_REQUESTS:
            r = requests.get(
                url, headers=DEFAULT_HEADERS, timeout=REQUEST_TIMEOUT, allow_redirects=True
            )
            r.raise_for_status()
            r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        req = urllib.request.Request(url, headers=DEFAULT_HEADERS)
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            return resp.read().decode(resp.headers.get_content_charset() or "utf-8")
    except Exception as e:
        logger.warning("拉取页面失败 %s: %s", url, e)
        return None


def resolve_download_url_from_page(page_url, size_filter):
    """
    从页面 URL 爬取 HTML，筛选出与 size_filter 匹配的下载链接（仅处理对应大小的链接）。
    :param page_url: 页面地址（如 https://testfile.org/file-1GB）
    :param size_filter: 大小标识，如 "1GB"，只保留链接或文本中包含该标识的下载地址
    :return: 解析出的第一个匹配的绝对下载 URL；无匹配或失败时返回 None
    """
    html = _fetch_page_html(page_url)
    if not html:
        return None

    # 匹配 <a href="...">...</a>，提取 href 与链接文本
    pattern = re.compile(
        r'<a\s+[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*>([^<]*)</a>',
        re.IGNORECASE | re.DOTALL
    )
    matches = pattern.findall(html)
    sf = size_filter.strip().upper()
    candidates = []

    for href, text in matches:
        href = href.strip()
        if not href or href.startswith("#") or href.startswith("javascript:"):
            continue
        full_url = urljoin(page_url, href)
        # 链接文本或 href 中包含 size_filter（不区分大小写）
        if sf in (href + " " + text).upper():
            candidates.append(full_url)

    # 优先选择看起来像直接文件链接的（含 file、download、.zip 等）
    for u in candidates:
        path = urlparse(u).path.upper()
        if "FILE" in path or "DOWNLOAD" in path or ".ZIP" in path or ".BIN" in path:
            logger.info("从页面筛选出 %s 下载链接: %s", size_filter, u)
            return u
    if candidates:
        logger.info("从页面筛选出 %s 下载链接: %s", size_filter, candidates[0])
        return candidates[0]
    return None


# ===================== 获取文件大小与文件名 =====================
def get_file_size(url):
    """
    获取远程文件大小（字节）。先尝试 HEAD，失败时回退为 GET（仅读响应头取 Content-Length）。
    :param url: 文件 URL
    :return: 文件大小（字节），失败返回 None
    """
    size = _head_content_length(url)
    if size is not None:
        return size
    # HEAD 失败时改为使用 GET 请求获取 Content-Length（不读 body）
    size = _get_content_length(url)
    if size is not None:
        return size
    return None


def get_filename_from_url(url):
    """
    从 URL 中解析出文件名（用于保存到 SAVE_DIR 时命名）。
    :param url: 文件 URL
    :return: 文件名，无法解析则返回 DEFAULT_FILENAME
    """
    if not url or "/" not in url:
        return DEFAULT_FILENAME
    # 去掉查询参数
    path = url.split("?")[0].rstrip("/")
    name = path.split("/")[-1]
    return name if name else DEFAULT_FILENAME


# ===================== 分块下载 =====================
def download_chunk(url, start_byte, end_byte, chunk_index, get_range_callable=None):
    """
    下载单个字节范围 [start_byte, end_byte]（含两端）。
    :param url: 文件 URL
    :param start_byte: 起始字节（含）
    :param end_byte: 结束字节（含）
    :param chunk_index: 块序号，用于结果排序
    :param get_range_callable: 可选；(url, start, end) -> bytes，用于连接池复用；为 None 时使用 _get_range
    :return: (chunk_index, bytes_content)，失败返回 (chunk_index, None)
    """
    get_range = get_range_callable if get_range_callable is not None else _get_range
    for attempt in range(CHUNK_RETRY_TIMES):
        try:
            data = get_range(url, start_byte, end_byte)
            return (chunk_index, data)
        except Exception as e:
            logger.warning("块 %s Range %s-%s 第 %s 次失败: %s", chunk_index, start_byte, end_byte, attempt + 1, e)
    return (chunk_index, None)


def download_file_multithreaded(url, save_path, num_threads, total_size):
    """
    多线程分块下载整个文件并按序写入 save_path。
    :param url: 文件 URL
    :param save_path: 本地保存完整路径
    :param num_threads: 线程数
    :param total_size: 文件总大小（字节）
    :return: (total_time_seconds, total_bytes_written)，若失败则 total_bytes_written 可能小于 total_size
    """
    if total_size <= 0:
        raise ValueError("文件大小必须大于 0")

    # 计算每块范围（尽量均分，最后一块可能略短）
    chunk_size = (total_size + num_threads - 1) // num_threads
    ranges = []
    s = 0
    for i in range(num_threads):
        if s >= total_size:
            break
        e = min(s + chunk_size - 1, total_size - 1)
        ranges.append((s, e, i))
        s = e + 1

    start_time = time.perf_counter()
    chunks = {}  # chunk_index -> bytes

    # 连接池控制：使用 requests 时创建 Session + HTTPAdapter，分块下载复用连接
    get_range_callable = None
    if HAS_REQUESTS:
        try:
            from requests.adapters import HTTPAdapter
            session = requests.Session()
            adapter = HTTPAdapter(
                pool_connections=POOL_CONNECTIONS,
                pool_maxsize=POOL_MAXSIZE
            )
            session.mount("http://", adapter)
            session.mount("https://", adapter)
            get_range_callable = lambda u, s, e: _get_range_via_session(session, u, s, e)
        except Exception as ex:
            logger.debug("连接池初始化失败，使用单次请求: %s", ex)

    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = {
            executor.submit(download_chunk, url, start, end, idx, get_range_callable): idx
            for start, end, idx in ranges
        }
        for future in as_completed(futures):
            idx, data = future.result()
            if data is None:
                logger.error("块 %s 下载失败", idx)
                continue
            chunks[idx] = data

    # 按块序号顺序写入文件
    os.makedirs(os.path.dirname(os.path.abspath(save_path)) or ".", exist_ok=True)
    written = 0
    try:
        with open(save_path, "wb") as f:
            for i in sorted(chunks.keys()):
                f.write(chunks[i])
                written += len(chunks[i])
    except OSError as e:
        logger.exception("写入文件失败: %s", e)

    total_time = time.perf_counter() - start_time
    return (total_time, written)


# ===================== 基准测试与绘图 =====================
def run_benchmark(url, save_dir, thread_counts=None, keep_file=True, do_plot=True):
    """
    对给定线程数列表依次进行下载测速，并可选绘制曲线图。
    :param url: 下载 URL
    :param save_dir: 本地保存目录（将在此目录下创建文件）
    :param thread_counts: 要测试的线程数列表，默认 THREAD_COUNTS
    :param keep_file: 每次测试后是否保留文件；False 则删除（最后一次可保留）
    :param do_plot: 是否绘制线程数 vs 速度曲线图
    :return: [(thread_count, speed_mbps, time_seconds), ...]
    """
    if thread_counts is None:
        thread_counts = THREAD_COUNTS

    total_size = get_file_size(url)
    if total_size is None or total_size <= 0:
        # 原地址不可用（如 403）时，尝试备用直链
        if FALLBACK_DIRECT_URL and url != FALLBACK_DIRECT_URL:
            logger.warning("原地址无法获取文件大小(可能 403)，正在尝试备用地址: %s", FALLBACK_DIRECT_URL)
            url = FALLBACK_DIRECT_URL
            total_size = get_file_size(url)
        if total_size is None or total_size <= 0:
            raise RuntimeError(
                "无法获取文件大小，请确认 URL 支持 HEAD/Content-Length 且支持 Range。"
                "可指定备用直链: python io_download_benchmark.py --url \"%s\""
                % (FALLBACK_DIRECT_URL or "http://speedtest.tele2.net/10MB.zip")
            )

    filename = get_filename_from_url(url)
    save_path_base = os.path.join(save_dir, filename)
    # 每次测试使用不同文件名避免覆盖：downloaded_file_1thread.bin 等
    results = []

    for num_threads in thread_counts:
        # 每次测试使用独立文件名，便于多次运行不覆盖
        suffix = f"{num_threads}threads"
        ext = os.path.splitext(filename)[1] or ".bin"
        base = os.path.splitext(filename)[0] or "downloaded"
        save_path = os.path.join(save_dir, f"{base}_{suffix}{ext}")

        logger.info("开始测试：线程数=%s，文件大小=%.2f MB", num_threads, total_size / (1024 * 1024))
        try:
            total_time, written = download_file_multithreaded(url, save_path, num_threads, total_size)
        except Exception as e:
            logger.exception("线程数 %s 测试失败: %s", num_threads, e)
            results.append((num_threads, 0.0, 0.0))
            continue

        speed_mbps = (written / (1024 * 1024)) / total_time if total_time > 0 else 0.0
        results.append((num_threads, speed_mbps, total_time))
        logger.info("线程数=%s，耗时=%.2f s，写入=%.2f MB，速度=%.2f MB/s", num_threads, total_time, written / (1024 * 1024), speed_mbps)

        if not keep_file and os.path.exists(save_path):
            try:
                os.remove(save_path)
            except OSError as e:
                logger.warning("删除临时文件失败: %s", e)

    if do_plot and results:
        _plot_speed_curve(results, save_dir)

    return results


def _setup_matplotlib_font():
    """设置 matplotlib 中文字体，避免乱码。"""
    try:
        import matplotlib
        import matplotlib.pyplot as plt
        if sys.platform == "win32":
            font_name = "Microsoft YaHei"
        elif sys.platform == "darwin":
            font_name = "PingFang SC"
        else:
            font_name = "WenQuanYi Micro Hei"
        plt.rcParams["font.sans-serif"] = [font_name]
        plt.rcParams["axes.unicode_minus"] = False
        return True
    except Exception as e:
        logger.warning("matplotlib 字体设置失败: %s", e)
        return False


def _plot_speed_curve(results, save_dir):
    """
    绘制「线程数 vs 下载速度(MB/s)」曲线图并保存到 save_dir。
    :param results: [(thread_count, speed_mbps, time_seconds), ...]
    :param save_dir: 图片保存目录
    """
    _setup_matplotlib_font()
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        logger.warning("未安装 matplotlib，跳过绘图。建议: pip install matplotlib")
        return

    # 再次确认字体（按用户规则在绘图函数开始时确认）
    _setup_matplotlib_font()

    threads = [r[0] for r in results]
    speeds = [r[1] for r in results]

    fig, ax = plt.subplots()
    ax.plot(threads, speeds, marker="o", linestyle="-", linewidth=2, markersize=6)
    ax.set_xlabel("线程数", fontsize=12)
    ax.set_ylabel("下载速度 (MB/s)", fontsize=12)
    ax.set_title("IO 密集型：多线程分块下载 — 线程数 vs 下载速度", fontsize=14)
    ax.grid(True, linestyle="--", alpha=0.7)
    ax.set_xlim(0, max(threads) * 1.05 if threads else 1)

    out_path = os.path.join(save_dir, "io_download_speed_curve.png")
    os.makedirs(save_dir, exist_ok=True)
    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close()
    logger.info("曲线图已保存: %s", out_path)


# ===================== 数据产出：表格与 JSON =====================
def output_data_report(results, save_dir, url="", file_size_mb=0.0):
    """
    输出数据报告：控制台表格 + 保存 JSON。
    :param results: [(thread_count, speed_mbps, time_seconds), ...]
    :param save_dir: 报告与 JSON 保存目录
    :param url: 下载 URL（可选，写入报告）
    :param file_size_mb: 文件大小 MB（可选）
    """
    print("\n=== 数据产出 ===")
    if not results:
        print("无测试数据。")
        return

    headers = ["线程数", "总耗时(秒)", "下载速度(MB/s)"]
    col_widths = [10, 14, 16]
    sep = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"
    head_row = "|" + "|".join(h.center(col_widths[i] + 2) for i, h in enumerate(headers)) + "|"
    print(sep)
    print(head_row)
    print(sep)
    for r in results:
        cells = [str(r[0]), f"{r[2]:.2f}", f"{r[1]:.2f}"]
        line = "|" + "|".join(cells[i].rjust(col_widths[i] + 2) for i in range(3)) + "|"
        print(line)
    print(sep)
    print(f"下载地址: {url or '(未记录)'}")
    print(f"文件大小: {file_size_mb:.2f} MB" if file_size_mb else "文件大小: (未记录)")
    print(f"保存目录: {save_dir}")
    curve_path = os.path.join(save_dir, "io_download_speed_curve.png")
    print(f"曲线图（线程数-总下载速度）: {curve_path}")

    records = [{"thread_count": r[0], "time_seconds": r[2], "speed_mbps": r[1]} for r in results]
    report = {"url": url, "file_size_mb": file_size_mb, "save_dir": save_dir, "records": records}
    out_json = os.path.join(save_dir, "io_download_report.json")
    try:
        os.makedirs(save_dir, exist_ok=True)
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
        print(f"已保存数据至: {out_json}")
    except OSError as e:
        logger.warning("保存 JSON 失败: %s", e)


# ===================== 性能分析 =====================
def output_performance_analysis(results):
    """
    基于测试数据输出性能分析：速度范围、最优线程数等。
    :param results: [(thread_count, speed_mbps, time_seconds), ...]
    """
    print("\n=== 性能分析 ===")
    if not results:
        print("无测试数据，无法分析。")
        return

    speeds = [r[1] for r in results]
    times = [r[2] for r in results]
    threads = [r[0] for r in results]
    best_idx = max(range(len(speeds)), key=lambda i: speeds[i])
    best_threads, best_speed, best_time = results[best_idx][0], results[best_idx][1], results[best_idx][2]

    print(f"  下载速度范围: {min(speeds):.2f} ~ {max(speeds):.2f} MB/s，平均 {sum(speeds)/len(speeds):.2f} MB/s")
    print(f"  总耗时范围: {min(times):.2f}s ~ {max(times):.2f}s")
    print(f"  本次测试中最佳: 线程数={best_threads} 时速度={best_speed:.2f} MB/s，耗时={best_time:.2f}s")
    print("  说明: 线程数增加通常可提升下载速度，直至受带宽或服务器限速制约后趋于平稳或略降。")


# ===================== 分析结论 =====================
def output_conclusion(results):
    """
    根据测试数据输出分析结论与选型建议。
    :param results: [(thread_count, speed_mbps, time_seconds), ...]
    """
    print("\n=== 分析结论 ===")
    if not results:
        print("无测试数据，无法给出结论。")
        return

    best_idx = max(range(len(results)), key=lambda i: results[i][1])
    best_threads = results[best_idx][0]
    best_speed = results[best_idx][1]

    print(f"  1. 在本测试条件下，线程数={best_threads} 时达到最高下载速度 {best_speed:.2f} MB/s。")
    print("  2. 实际使用时可在此线程数附近微调；线程过多可能受带宽或服务器限制，收益有限。")
    print("  3. 文件已按配置保存到指定路径（如 F 盘）；曲线图与 JSON 报告同目录。")
    print()


# ===================== 主入口 =====================
def main():
    import argparse
    parser = argparse.ArgumentParser(description="IO 密集型：多线程分块下载测速并绘制线程数-速度曲线")
    parser.add_argument("--url", default=DOWNLOAD_URL, help="大文件 URL（需支持 Range）")
    parser.add_argument("--output-dir", "-o", default=SAVE_DIR, help="本地下载目录")
    parser.add_argument("--thread-counts", type=str, default=",".join(map(str, THREAD_COUNTS)),
                        help="逗号分隔的线程数列表，如 1,2,5,10,20,50")
    parser.add_argument("--keep", action="store_true", help="测试后保留下载文件")
    parser.add_argument("--no-keep", action="store_true", help="测试后删除下载文件（默认不删除）")
    parser.add_argument("--no-plot", action="store_true", help="不绘制曲线图")
    args = parser.parse_args()

    url = (args.url or "").strip()
    # 若为页面链接（如 testfile.org/file-1GB），则爬取页面并筛选出与 DOWNLOAD_SIZE_FILTER 对应的下载链接
    resolved = resolve_download_url_from_page(url, DOWNLOAD_SIZE_FILTER)
    if resolved:
        logger.info("使用筛选后的 1GB 下载链接进行测速")
        url = resolved
    # 统一为规范路径，确保 F 盘等绝对路径正确（支持 F:/ 或 F:\）
    save_dir = (args.output_dir or "").strip().replace("\\", "/")
    if not url:
        logger.error("未配置下载地址。请在脚本内填写 DOWNLOAD_URL，或使用 --url 参数。")
        sys.exit(1)
    if not save_dir:
        logger.error("未配置本地下载目录。请在脚本内填写 SAVE_DIR，或使用 --output-dir 参数。")
        sys.exit(1)

    try:
        thread_counts = [int(x.strip()) for x in args.thread_counts.split(",") if x.strip()]
    except ValueError:
        logger.error("--thread-counts 格式错误，应为逗号分隔的整数，如 1,2,5,10")
        sys.exit(1)
    if not thread_counts:
        thread_counts = THREAD_COUNTS

    keep_file = args.keep or not args.no_keep
    do_plot = not args.no_plot

    # 运行环境与使用方法
    _print_runtime_env()
    print_usage()

    # 获取文件大小（用于报告）
    total_size = get_file_size(url)
    file_size_mb = (total_size / (1024 * 1024)) if total_size and total_size > 0 else 0.0

    # 基准测试（下载到 save_dir，如 F:/下载测试）
    results = run_benchmark(url, save_dir, thread_counts=thread_counts, keep_file=keep_file, do_plot=do_plot)

    # 数据产出
    output_data_report(results, save_dir, url=url, file_size_mb=file_size_mb)

    # 优缺点说明
    print_pros_cons()

    # 性能分析
    output_performance_analysis(results)

    # 分析结论
    output_conclusion(results)


if __name__ == "__main__":
    main()
