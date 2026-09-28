# -*- coding: utf-8 -*-
"""检查已安装包页面的 JS 是否有语法问题（一次性脚本）。"""
import re
import sys

sys.stdout.reconfigure(encoding="utf-8")

src = open(r"C:\Users\yangchaoxin\AppData\Roaming\uv\tools\agent-runtime-manager"
           r"\Lib\site-packages\arm\ui\server.py", encoding="utf-8").read()
i = src.index('_HTML = r"""') + len('_HTML = r"""')
j = src.index("</html>")
html = src[i:j]
js = html.split("<script>")[1].split("</script>")[0]
print("JS 长度:", len(js))

m = re.search(r"copySid\(this,[^)]*\)", js)
print("copySid 调用样例:", (m.group(0)[:130] if m else "NOT FOUND"))

m2 = re.search(r"resumeSession\(this,[^)]*\)", js)
print("resumeSession 调用样例:", (m2.group(0)[:130] if m2 else "NOT FOUND"))

# 引号配平粗检
print("单引号奇偶(0=偶):", js.count("'") % 2)
