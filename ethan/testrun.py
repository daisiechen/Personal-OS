"""按测试文件分几路同时跑测试（部署前串行跑 1600 多个测试要 8 分钟，她嫌上线慢）。

- 每个 tests/test_*.py 是一份活，几个子进程从队列里领，跑完一份领下一份；大文件先发，免得最后剩一个大的拖着。
- 子进程各有各的全局变量和环境变量，互不干扰；同一个子进程里先后跑的文件，和以前串行时一样共用一个进程。
- 输出和 unittest 一样：挂掉的写“FAIL: 测试名 (文件.类.测试名)”（autodeploy.failed_tests 靠它找名字），最后“Ran N tests”“OK / FAILED”，有挂的退出码 1。
- 跑完一个文件，ethan 的模块上还挂着假对象（Mock）就算这个文件失败：串行时碰巧没事的补丁泄漏，分路后会随机挂别人。
- 某个子进程整个崩了（被杀、段错误），没跑完的文件各自单独重跑一遍，真正崩的那个算失败、写明是哪个文件。

运行：python3 -m ethan.testrun [-j 路数] [-v | -q]（默认路数 = CPU 核数）；单线程的老入口 python3 -m unittest discover -s tests -v 照样能用。
"""

import argparse
import io
import os
import sys
import time
import unittest
from concurrent.futures import ProcessPoolExecutor, as_completed
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path

TESTS = Path(__file__).resolve().parent.parent / "tests"


def modules(tests: Path = TESTS) -> list:
    """所有测试文件的模块名，大的在前（大文件一般跑得久）。"""
    files = sorted(tests.glob("test_*.py"), key=lambda p: (-p.stat().st_size, p.name))
    return [p.stem for p in files]


def leaked_mocks() -> set:
    """ethan 各模块上还挂着的假对象（“模块.名字”）。测试打的补丁没撤，或者补丁期间头一次 import 的模块
    用 from … import 把假的抓进了自己（11-07：test_questions 补丁 clock.her_today 时 memsearch 头一次加载，
    之后同一个进程里 memsearch 的“今天”一直是假的）——串行时碰巧没事，换个顺序就挂，这里直接报出来。"""
    from unittest import mock
    out = set()
    for name, mod in list(sys.modules.items()):
        if name == "ethan" or name.startswith(("ethan.", "migrations")):
            for attr, value in list(vars(mod).items()):
                if isinstance(value, mock.NonCallableMock):
                    out.add(f"{name}.{attr}")
    return out


def run_module(name: str, tests: str, verbosity: int) -> dict:
    """在子进程里跑一个测试文件，返回统计和 unittest 的文字报告。"""
    if tests not in sys.path:
        sys.path.insert(0, tests)
    stream = io.StringIO()
    started = time.time()
    before = leaked_mocks()
    suite = unittest.defaultTestLoader.loadTestsFromName(name)  # 导入失败（语法错、缺模块）unittest 自己记成一条 ERROR
    result = unittest.TextTestRunner(stream=stream, verbosity=verbosity).run(suite)
    leaked = sorted(leaked_mocks() - before)
    report = stream.getvalue()
    if leaked:
        report = ("=" * 70 + f"\nERROR: {name} (跑完留下了假对象)\n" + "-" * 70 + "\n"
                  + "这些模块属性跑完还是 Mock，会让同一进程里后跑的测试出错：" + "、".join(leaked) + "\n\n" + report)
    return {"name": name, "run": result.testsRun, "failures": len(result.failures), "errors": len(result.errors) + bool(leaked),
            "skipped": len(result.skipped), "ok": result.wasSuccessful() and not leaked, "seconds": time.time() - started,
            "report": report}


def _details(report: str) -> str:
    """unittest 报告里“======” 开头的失败详情段（去掉每个文件自己的 Ran/OK 收尾）。"""
    start = report.find("=" * 70)
    if start < 0:
        return ""
    end = report.rfind("-" * 70)
    return report[start:end].rstrip() + "\n"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="按测试文件分几路同时跑测试")
    parser.add_argument("-j", "--jobs", type=int, default=os.cpu_count() or 1)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-q", "--quiet", action="store_true")
    parser.add_argument("-s", "--start", type=Path, default=TESTS, help="测试目录（默认仓库的 tests/）")
    parser.add_argument("names", nargs="*", help="只跑这几个文件（test_xxx），不写就全跑")
    args = parser.parse_args(argv)
    verbosity = 2 if args.verbose else 0 if args.quiet else 1
    names = args.names or modules(args.start)
    started = time.time()
    results, crashed = [], []
    start = str(args.start.resolve())

    def show(r):
        results.append(r)
        if args.verbose:
            sys.stderr.write(r["report"].split("-" * 70)[0])
        elif not args.quiet:
            sys.stderr.write("." if r["ok"] else "F")
        sys.stderr.flush()

    unfinished = []
    with ProcessPoolExecutor(max_workers=max(1, min(args.jobs, len(names)))) as pool:
        futures = {pool.submit(run_module, n, start, verbosity): n for n in names}
        for fut in as_completed(futures):
            try:
                show(fut.result())
            except BrokenProcessPool:  # 有个子进程整个死了，没跑完的都会报这个：下面一个个重跑，找出真正崩的
                unfinished.append(futures[fut])
    for n in sorted(unfinished, key=names.index):
        with ProcessPoolExecutor(max_workers=1) as pool:
            try:
                show(pool.submit(run_module, n, start, verbosity).result())
            except BrokenProcessPool as e:
                crashed.append(f"{n}（子进程崩了：{e}）")
    bad = [r for r in results if not r["ok"]]
    out = sys.stderr
    if not args.verbose and not args.quiet:
        out.write("\n")
    for r in sorted(bad, key=lambda r: r["name"]):
        out.write(_details(r["report"]))
    for c in crashed:
        out.write("=" * 70 + f"\nERROR: {c.split('（')[0]} (整个文件没跑完)\n{c}\n")
    run = sum(r["run"] for r in results)
    failures, errors = sum(r["failures"] for r in results), sum(r["errors"] for r in results) + len(crashed)
    skipped = sum(r["skipped"] for r in results)
    slow = "、".join(f"{r['name']} {r['seconds']:.0f}s" for r in sorted(results, key=lambda r: -r["seconds"])[:5])
    out.write("-" * 70 + f"\nRan {run} tests in {time.time() - started:.3f}s（{len(names)} 个文件，{args.jobs} 路；最慢：{slow}）\n\n")
    if failures or errors:
        parts = [f"failures={failures}"] * bool(failures) + [f"errors={errors}"] * bool(errors) + [f"skipped={skipped}"] * bool(skipped)
        out.write(f"FAILED ({', '.join(parts)})\n")
        return 1
    out.write("OK" + (f" (skipped={skipped})" if skipped else "") + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
