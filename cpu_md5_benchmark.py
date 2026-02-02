# -*- coding: utf-8 -*-
"""
CPU 密集型基准测试：多进程并发计算目录下所有文件的 MD5，测试不同进程数下的总耗时。

【功能】
  - 遍历指定目录下的文件，每个文件交给一个进程计算 MD5（ProcessPoolExecutor，绕过 GIL）。
  - 进程数从 1 测到 2×CPU 核数，绘制「进程数 vs 总耗时」曲线图。
  - 本机：可指定几百～上千个文件、总大小十几 GB 以上的大目录（--input-dir）。
  - CI：使用 --generate 生成测试数据，配合 --run-timeout 90 使主流程约 5 分钟内完成。

【使用方法】
  - 本机大目录：python cpu_md5_benchmark.py --input-dir /path/to/large/dir
  - CI（约 5 分钟）：python cpu_md5_benchmark.py --generate --generate-files 80 --generate-size-mb 0.5 --run-timeout 90
  - 收集结果：从 Actions 运行页下载「cpu-benchmark-results」artifact，解压即得 JSON + 曲线图。
"""
import os
import sys
import json
import time
import hashlib
import logging
import argparse
import platform
import subprocess
from pathlib import Path
from concurrent.futures import ProcessPoolExecutor, as_completed, TimeoutError as FuturesTimeoutError

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger(__name__)

# 单文件读块大小（字节）
CHUNK_SIZE = 8192

# 超时控制（秒）
FILE_MD5_TIMEOUT = 120   # 单文件 MD5 计算超时
RUN_TIMEOUT = 600        # 单轮（某进程数下）总超时，超时后跳过剩余文件并记录；CI 可传 --run-timeout 90 控制在约 5 分钟内


def get_cpu_model():
    """
    获取当前 CPU 型号字符串（用于报告与日志）。
    :return: CPU 型号描述，失败时返回 platform.processor() 或未知
    """
    try:
        if sys.platform == "linux":
            with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if line.strip().startswith("model name"):
                        return line.split(":", 1)[1].strip()
        elif sys.platform == "win32":
            r = subprocess.run(
                ["wmic", "cpu", "get", "name"],
                capture_output=True, text=True, timeout=5, creationflags=0x08000000 if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
            )
            if r.returncode == 0 and r.stdout:
                lines = [x.strip() for x in r.stdout.strip().splitlines() if x.strip() and x.strip().lower() != "name"]
                if lines:
                    return lines[0]
        elif sys.platform == "darwin":
            r = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout:
                return r.stdout.strip()
    except Exception as e:
        logger.debug("获取 CPU 型号失败: %s", e)
    return platform.processor() or "未知"


def _compute_file_md5(filepath):
    """
    计算单个文件的 MD5（分块读，避免大文件占满内存）。
    供子进程调用，仅接受可序列化参数。
    :param filepath: 文件路径（str）
    :return: (filepath, md5_hex) 或 (filepath, None, error_str)
    """
    try:
        h = hashlib.md5()
        with open(filepath, "rb") as f:
            while True:
                data = f.read(CHUNK_SIZE)
                if not data:
                    break
                h.update(data)
        return (filepath, h.hexdigest())
    except Exception as e:
        return (filepath, None, str(e))


def _collect_files(root_dir):
    """收集目录下所有文件路径（递归），跳过目录和非常规文件。"""
    root = Path(root_dir)
    if not root.is_dir():
        return []
    files = []
    for p in root.rglob("*"):
        if p.is_file():
            try:
                files.append(str(p.resolve()))
            except OSError:
                pass
    return files


def run_benchmark(file_list, process_counts=None, output_dir=None, do_plot=True, run_timeout=None, file_timeout=None):
    """
    对给定进程数列表依次运行多进程 MD5 计算，并可选绘图。
    :param file_list: 文件路径列表
    :param process_counts: 进程数列表，默认 [1, 2, 4, ..., 2*cpu_count]
    :param output_dir: 结果与图片保存目录
    :param do_plot: 是否绘制进程数-耗时曲线图
    :param run_timeout: 单轮总超时（秒），None 则用 RUN_TIMEOUT；CI 可传 90 使主流程约 5 分钟内完成
    :param file_timeout: 单文件 MD5 超时（秒），None 则用 FILE_MD5_TIMEOUT
    :return: [(process_count, total_time_seconds, file_count), ...]
    """
    run_limit = run_timeout if run_timeout is not None else RUN_TIMEOUT
    file_limit = file_timeout if file_timeout is not None else FILE_MD5_TIMEOUT
    n_cpu = os.cpu_count() or 4
    if process_counts is None:
        process_counts = []
        x = 1
        while x <= 2 * n_cpu:
            process_counts.append(x)
            x = min(x * 2, 2 * n_cpu)
        if process_counts[-1] != 2 * n_cpu and (2 * n_cpu) not in process_counts:
            process_counts.append(2 * n_cpu)
        process_counts = sorted(set(process_counts))

    if not file_list:
        logger.warning("文件列表为空，跳过基准测试")
        return []

    results = []
    for n_proc in process_counts:
        start = time.perf_counter()
        done = 0
        timeout_count = 0
        run_timed_out = False
        with ProcessPoolExecutor(max_workers=n_proc) as executor:
            futures = {executor.submit(_compute_file_md5, f): f for f in file_list}
            for future in as_completed(futures):
                if time.perf_counter() - start > run_limit:
                    run_timed_out = True
                    logger.warning("单轮超时（%s 秒），进程数=%s，已跳过剩余任务", run_limit, n_proc)
                    break
                try:
                    r = future.result(timeout=file_limit)
                except FuturesTimeoutError:
                    timeout_count += 1
                    logger.warning("单文件超时（%s 秒）: %s", file_limit, futures.get(future, "?"))
                    continue
                if len(r) == 2:
                    done += 1
                else:
                    logger.warning("文件 %s 失败: %s", r[0], r[2])
        total_time = time.perf_counter() - start
        results.append((n_proc, total_time, len(file_list), done))
        logger.info("进程数=%s，总耗时=%.2f s，成功=%s/%s，超时=%s%s",
                    n_proc, total_time, done, len(file_list), timeout_count,
                    "（已提前结束）" if run_timed_out else "")

    if do_plot and results and output_dir:
        _plot_curve(results, output_dir)
    return results


def _setup_matplotlib_font():
    """设置 matplotlib 中文字体。"""
    try:
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


def _plot_curve(results, output_dir):
    """绘制进程数 vs 总耗时曲线图并保存到 output_dir。"""
    _setup_matplotlib_font()
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        logger.warning("未安装 matplotlib，跳过绘图")
        return
    _setup_matplotlib_font()

    procs = [r[0] for r in results]
    times = [r[1] for r in results]
    fig, ax = plt.subplots()
    ax.plot(procs, times, marker="o", linestyle="-", linewidth=2, markersize=6)
    ax.set_xlabel("进程数", fontsize=12)
    ax.set_ylabel("总耗时 (秒)", fontsize=12)
    ax.set_title("CPU 密集型：多进程 MD5 — 进程数 vs 总耗时", fontsize=14)
    ax.grid(True, linestyle="--", alpha=0.7)
    ax.set_xlim(0, max(procs) * 1.05 if procs else 1)
    os.makedirs(output_dir, exist_ok=True)
    out_path = os.path.join(output_dir, "cpu_md5_curve.png")
    plt.tight_layout()
    plt.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close()
    logger.info("曲线图已保存: %s", out_path)


def generate_test_files(output_dir, num_files=200, file_size_mb=1):
    """
    生成测试用文件（供 CI 使用，无需本机大目录）。
    :param output_dir: 输出目录
    :param num_files: 文件数量
    :param file_size_mb: 每个文件大小（MB）
    """
    os.makedirs(output_dir, exist_ok=True)
    chunk = b"x" * 1024 * 1024  # 1MB
    to_write = int(file_size_mb * 1024 * 1024)  # 支持小数 MB（如 0.5）
    written = 0
    for i in range(num_files):
        path = os.path.join(output_dir, f"test_{i:05d}.bin")
        with open(path, "wb") as f:
            while written < to_write:
                n = min(len(chunk), to_write - written)
                f.write(chunk[:n])
                written += n
            written = 0
        if (i + 1) % 50 == 0:
            logger.info("已生成 %s/%s 个文件", i + 1, num_files)
    logger.info("测试数据已生成: %s，共 %s 个文件，约 %s MB", output_dir, num_files, num_files * file_size_mb)


def main():
    parser = argparse.ArgumentParser(
        description="CPU 密集型：多进程 MD5 基准测试，适合在 GitHub Actions 运行，本机收集结果"
    )
    parser.add_argument("--input-dir", "-i", help="待计算 MD5 的目录（与 --generate 二选一）")
    parser.add_argument("--output-dir", "-o", default="./cpu_benchmark_results",
                        help="结果与曲线图输出目录，默认 ./cpu_benchmark_results")
    parser.add_argument("--generate", action="store_true",
                        help="在 output-dir 下生成测试文件（供 CI 使用），再跑基准测试")
    parser.add_argument("--generate-files", type=int, default=200, help="--generate 时生成的文件数")
    parser.add_argument("--generate-size-mb", type=float, default=1.0, help="--generate 时每个文件大小(MB)，可为小数如 0.5")
    parser.add_argument("--process-counts", type=str, default="",
                        help="逗号分隔的进程数，如 1,2,4,8；默认 1,2,4,...,2*cpu_count")
    parser.add_argument("--run-timeout", type=int, default=None,
                        help="单轮总超时（秒），CI 可传 90 使主流程约 5 分钟内完成")
    parser.add_argument("--file-timeout", type=int, default=None,
                        help="单文件 MD5 超时（秒）")
    parser.add_argument("--no-plot", action="store_true", help="不绘制曲线图")
    args = parser.parse_args()

    output_dir = args.output_dir.rstrip("/\\")
    os.makedirs(output_dir, exist_ok=True)

    if args.generate:
        data_dir = os.path.join(output_dir, "testdata")
        generate_test_files(data_dir, args.generate_files, args.generate_size_mb)
        input_dir = data_dir
    else:
        input_dir = (args.input_dir or "").strip()
        if not input_dir or not os.path.isdir(input_dir):
            logger.error("请指定 --input-dir 或使用 --generate 在 CI 下生成测试数据")
            sys.exit(1)

    file_list = _collect_files(input_dir)
    if not file_list:
        logger.error("未找到任何文件")
        sys.exit(1)
    cpu_model = get_cpu_model()
    logger.info("测试 CPU: %s（逻辑核数: %s）", cpu_model, os.cpu_count())
    logger.info("共 %s 个文件，总大小约 %.2f MB", len(file_list),
                sum(os.path.getsize(f) for f in file_list) / (1024 * 1024))

    process_counts = None
    if args.process_counts:
        try:
            process_counts = [int(x.strip()) for x in args.process_counts.split(",") if x.strip()]
        except ValueError:
            logger.error("--process-counts 格式错误")
            sys.exit(1)

    results = run_benchmark(
        file_list,
        process_counts=process_counts,
        output_dir=output_dir,
        do_plot=not args.no_plot,
        run_timeout=args.run_timeout,
        file_timeout=args.file_timeout
    )

    # 写出 JSON 报告（供本机收集），含 CPU 型号
    cpu_model = get_cpu_model()
    report = {
        "cpu_model": cpu_model,
        "cpu_count": os.cpu_count(),
        "file_count": len(file_list),
        "total_size_mb": round(sum(os.path.getsize(f) for f in file_list) / (1024 * 1024), 2),
        "records": [{"process_count": r[0], "time_seconds": r[1], "success": r[3], "total": r[2]} for r in results]
    }
    report_path = os.path.join(output_dir, "cpu_md5_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    logger.info("报告已保存: %s", report_path)


if __name__ == "__main__":
    main()
