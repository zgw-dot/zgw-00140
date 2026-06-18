import sys
import os
import shutil

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from verify_handoff_sealer import (
    TEST_ROOT_BASE,
    test_scenario_8_bug1_resume_without_pack_cli,
    test_scenario_9_bug2_phase_resume_mismatch,
    test_scenario_10_windows_terminal_output,
)


def main():
    if os.path.exists(TEST_ROOT_BASE):
        shutil.rmtree(TEST_ROOT_BASE, ignore_errors=True)
    os.makedirs(TEST_ROOT_BASE, exist_ok=True)

    print("=" * 70)
    print("运行回归测试 - 验证 Bug1 和 Bug2 检测能力")
    print("=" * 70)

    results = {}

    print("\n" + "=" * 70)
    print(">>> 测试 1: Bug1 检测 - --resume 无 --pack")
    print("=" * 70)
    try:
        test_scenario_8_bug1_resume_without_pack_cli()
        results["bug1"] = ("FAIL", "Bug1 未被检测到 - 测试可能已修复或测试逻辑有误")
    except AssertionError as e:
        if "BUG1" in str(e):
            results["bug1"] = ("PASS", f"Bug1 成功检测: {e}")
        else:
            results["bug1"] = ("ERROR", f"测试异常: {e}")
    except Exception as e:
        results["bug1"] = ("ERROR", f"测试异常: {e}")

    print("\n" + "=" * 70)
    print(">>> 测试 2: Bug2 检测 - phase/resume_available/汇总状态不一致")
    print("=" * 70)
    try:
        test_scenario_9_bug2_phase_resume_mismatch()
        results["bug2"] = ("FAIL", "Bug2 未被检测到 - 测试可能已修复或测试逻辑有误")
    except AssertionError as e:
        if "BUG2" in str(e):
            results["bug2"] = ("PASS", f"Bug2 成功检测: {e}")
        else:
            results["bug2"] = ("ERROR", f"测试异常: {e}")
    except Exception as e:
        results["bug2"] = ("ERROR", f"测试异常: {e}")

    print("\n" + "=" * 70)
    print(">>> 测试 3: Windows 终端输出验证")
    print("=" * 70)
    try:
        test_scenario_10_windows_terminal_output()
        results["terminal"] = ("PASS", "Windows 终端输出测试通过")
    except AssertionError as e:
        results["terminal"] = ("FAIL", f"Windows 终端输出测试失败: {e}")
    except Exception as e:
        results["terminal"] = ("ERROR", f"测试异常: {e}")

    print("\n" + "=" * 70)
    print("测试结果汇总")
    print("=" * 70)
    print(f"{'测试项':<30} {'结果':<10} 详情")
    print("-" * 70)
    for name, (status, detail) in results.items():
        marker = "✅" if status == "PASS" else "❌" if status == "FAIL" else "⚠️"
        print(f"{name:<30} {marker} {status:<8} {detail[:80]}")
    print("=" * 70)

    bug1_ok = results["bug1"][0] == "PASS"
    bug2_ok = results["bug2"][0] == "PASS"
    terminal_ok = results["terminal"][0] == "PASS"

    print(f"\nBug1 检测: {'✅ 成功' if bug1_ok else '❌ 失败'}")
    print(f"Bug2 检测: {'✅ 成功' if bug2_ok else '❌ 失败'}")
    print(f"终端输出: {'✅ 通过' if terminal_ok else '❌ 失败'}")

    if bug1_ok and bug2_ok:
        print("\n🎉 回归测试全部通过！两个 Bug 都能被稳定检测到。")
        return 0
    else:
        print("\n⚠️  部分测试未通过，请检查上方日志。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
