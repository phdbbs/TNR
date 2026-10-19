"""静态资源 URL：把版本号交给内容哈希，不再靠人工记日期。

模板里原先一律写 `?v=20260918k` 这类**人工日期串**，第五十二轮电脑端巡检
撞上了它的代价：改了 `static/js/portals/shelter.js`，浏览器仍然跑缓存里的旧
文件，页面看起来「改了没生效」，而所有源码判据都是绿的 —— 因为它们查的是
磁盘上的文件，不是浏览器里正在跑的那一份。人工升版本号这件事本身就是缺陷：
12 个引入点、每次都靠人记得，漏一个就只有一个端读到旧文件。

`?v=` 只影响缓存键、不影响取哪个文件（`/static/...` 的路径部分才决定），
所以这里把它换成「文件内容的哈希」：内容没变 → 哈希没变 → 继续用缓存；
内容变了 → 哈希变 → 自动重新拉取。没有需要人工维护的清单。
"""
import hashlib
import os

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static

register = template.Library()

#: `路径 → ((mtime_ns, 字节数), 哈希)`。用这两个值做失效判据，
#: 这样每次渲染只 `stat` 一次，不必把 200 KB 的门户 JS 重读一遍。
_STAMP_CACHE = {}


def asset_stamp(path):
    """返回静态文件的内容哈希（短串）。找不到文件时返回 `'0'`。

    `'0'` 而不是抛错：本标签只服务于缓存键，静态文件缺失应当在
    `collectstatic` / 页面 404 上暴露，而不是让整页渲染 500。

    ⚠ 失效判据用 `st_mtime_ns` + 字节数，**不要**取整到秒：同一秒内连改两次
    （编辑器保存、`git checkout`、脚本批量写文件都会）会命中缓存里的旧哈希，
    于是「改了没生效」以另一种形式回来了 —— 那正是本文件要消灭的东西。
    """
    found = finders.find(path)
    if not found:
        return '0'
    try:
        st = os.stat(found)
    except OSError:
        return '0'
    marker = (st.st_mtime_ns, st.st_size)
    cached = _STAMP_CACHE.get(path)
    if cached and cached[0] == marker:
        return cached[1]
    with open(found, 'rb') as f:
        stamp = hashlib.sha1(f.read()).hexdigest()[:12]
    _STAMP_CACHE[path] = (marker, stamp)
    return stamp


@register.simple_tag
def asset_url(path):
    """`{% asset_url 'js/portals/shelter.js' %}` → `/static/js/portals/shelter.js?v=<哈希>`。"""
    base = static(path.lstrip('/'))
    sep = '&' if '?' in base else '?'
    return f'{base}{sep}v={asset_stamp(path)}'
