# -*- coding: utf-8 -*-
"""构建信息占位文件。

源码运行/本地打包时就是这个占位值（不显示构建信息）。
GitHub Actions 构建前会重写本文件，写入版本、构建时间、commit，
exe 首屏于是能自报"我是哪次构建出来的"——排查"用户跑的到底是哪个包"
时这是最硬的证据。
"""

BUILD_INFO = {
    "version": "dev",
    "built_at": "",
    "commit": "",
}
