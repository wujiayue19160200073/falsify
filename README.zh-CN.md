# falsify

**你的测试是绿的。但它们是真的吗？**

`falsify` 故意把你的代码改坏，然后看有没有人发现。如果没人发现，它会把
**你的测试从没试过的那个输入** 找出来，连同上能补的断言一起给你。

零依赖，Python 3.9+，只测这次改动的那几个函数。

```
  SURVIVED [critical]  M001  boundary  line 14
    apply_discount  off-by-one: >= → > (behaviour changes exactly at the threshold)
    >=  →  >

    your tests never tried this input:
      apply_discount(200.0, 0.0, True)
      original  →  180.0
      mutant    →  200.0

    one line closes it:
      assert apply_discount(200.0, 0.0, True) == 180.0
```

（终端输出是英文的——这个工具面向的是全球开发者，报告要能直接贴进 PR。
中文文档在这里，输出保持英文。）

上面的输出来自 [`demo/`](demo/)。那个模块行覆盖率接近 100%，测试全绿。
如果你把一半的计价逻辑反过来，它的测试一条都不会红。

---

## 覆盖率一直没有告诉你的那件事

行覆盖率统计的是「哪些行**跑过**」。它完全不说明「哪些行**改错了会红**」。

自从 Agent 开始同时写代码和写测试，这两个数字就分家了：

| | 人手写的测试 | Agent 写的测试 |
| --- | --- | --- |
| 行覆盖率 | 约 75% | **100%** |
| 变异分数（真能抓住的缺陷比例） | 约 70% | **4%**（Wang et al., 2025） |

对着实现写出来的测试，断言的只是「实现做了什么」。它是一句带绿色对勾的重言式。
覆盖率一路涨，安全网一路薄，直到生产环境替你发现。

Thoughtworks 在 2026 年 4 月把变异测试放进 Technology Radar 的 **Trial** 环，
理由就是这个：它是唯一能回答「你的测试到底断言了什么」的测量方式。

那为什么几乎没人跑？

**因为现有工具慢到塞不进任何流程。** 全仓库变异意味着把整个测试套件重跑几百遍，
那是 40 分钟的夜间任务，第二个迭代就被删掉了。

`falsify` 只改一件事：**只变异这次改动碰到的函数。**
一个 PR 通常碰 1–5 个函数，这就是两分钟的任务，可以放进 CI，每个 PR 都跑。

---

## 它和别的变异测试工具哪里不一样

所有变异测试工具都会告诉你「有变异体存活」。那只是个数字，会被划过去。

`falsify` 告诉你 **是哪个输入** 漏了 —— 它让原始函数和变异后的函数并排跑遍
一个自动生成的输入空间，直到两者行为分岔。那个输入，就是你缺的那条测试。

这也意味着一条断言常常能补掉好几个洞：

```
  6 survivors, 3 assertions close every one of them.
```

---

## 安装

```bash
pip install git+https://github.com/wujiayue19160200073/falsify
```

不想装也行：

```bash
git clone https://github.com/wujiayue19160200073/falsify && cd falsify
PYTHONPATH=src python -m falsify.cli run --file demo/shop.py
```

## 用法

```bash
# 只测这个分支相对 main 改动的函数
falsify run --base main

# 测一个文件的全部函数
falsify run --file src/shop/pricing.py

# 只测一个函数（更快）
falsify run --file src/shop/pricing.py --function apply_discount

# CI：变异分数低于 70% 就失败
falsify run --base origin/main --ci --fail-under 70
```

输出格式：

```bash
falsify run --base main --md      # Markdown，直接贴进 PR 评论
falsify run --base main --json    # 机器可读，schema 为 "falsify/1"
falsify run --base main --badge docs/falsify.svg   # 生成徽章，提交到 README
```

想搞懂每类变异为什么危险：

```bash
falsify explain boundary
falsify explain compare
```

---

## 它不做什么

把边界说清楚，比列一堆功能有用。

- **它不做 AI 代码检测。** 没有作者分类器，没有「AI 味评分」。靠文本特征猜
  AI 写没写，结论不可靠而且往往言之凿凿；`falsify` 改的是**行为**，这个可以客观验证。
- **它不替代 linter，也不替代 reviewer。** 变异体存活的意思是「你的测试没抓到」，
  不是「这段代码错了」。
- **它不碰你这次没改的代码。** 这是设计，不是限制。
- **它不是一个沙箱。** 它会改写你的源文件、跑你的测试；在找反例阶段还会在
  子进程里 import 你的模块。它在 `finally` 里恢复文件，并在 `.falsify/backup`
  留一份磁盘备份 —— 但它假定你愿意让它动你的工作区。

## 安全边界

变异测试必须往源文件里写坏代码，这是这个工具唯一真正危险的地方，所以：

1. 目标文件有未提交改动时，**拒绝运行**（`--force` 可强制）。
2. 改之前先把文件复制到 `.falsify/backup/`。
3. 在 `finally` 里恢复原文 —— 包括 `Ctrl-C` —— 恢复后还做哈希校验。
4. 如果被强杀到 `finally` 都没跑，`falsify restore` 从备份还原。

它仍然会执行你的测试套件，也会 import 你的模块。如果模块有 import 期副作用
（联网、连库、发邮件），加 `--no-witness`：变异测试本身只跑**你的测试**，
只有反例搜索需要把模块 import 两遍。

---

## 实现要点

```
git diff  ──▶  哪些函数被改了？          (diff.py，AST 行号范围)
          ──▶  一个 token 一个 token 地改坏 (mutate.py，精确源码区间)
          ──▶  跑相关测试                (runner.py，限定范围的测试发现)
          ──▶  存活？去找反例             (witness.py，子进程沙箱)
          ──▶  给出缺的那条断言           (report.py)
```

两个刻意的取舍，值得单独说：

**变异改的是源码文本，不是 AST。** `ast.unparse` 会把整个文件重新打印一遍 ——
丢注释、重排代码、挪动行号。一个会重排你仓库的工具，你会用一次就再也不用了。
每一个变异只替换一段精确的 token，所以变异后的文件和原文件只差几个字符。

**测试发现是启发式的，而且它会承认。** `falsify` 找的是「import 了被测模块的
测试文件」。如果一个都没找到，它会明说，然后跑全量 —— 而不是用一个根本没跑过的
测试集，给你一个看起来很确定的分数。

---

## 和其他工具的关系

| | 范围 | 耗时 | 告诉你该怎么修 |
| --- | --- | --- | --- |
| `mutmut`、`cosmic-ray` | 全仓库 | 几分钟到几小时 | 不告诉 |
| Stryker、PIT | 整个模块 | 几分钟 | 不告诉 |
| **`falsify`** | **改动的函数** | **几秒到几分钟** | **给断言** |

`falsify` 不是想做「更好的 mutmut」。全仓库变异测试是对关键服务跑夜间任务的正解；
`falsify` 面向的是另外 95% 的 PR —— 那些「晚上再跑」等于「永远不会跑」的 PR。

---

## 许可

MIT，见 [LICENSE](LICENSE)。
