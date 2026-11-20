# GitHub 云端打包：上手指南（5 分钟，一次配置，永久受益）

> 目标：以后**不再需要在自己电脑上装 Python、跑打包脚本**。
> GitHub 免费替你打包，你只管下载 exe、双击。

## 一次性准备（5 分钟）

### 1. 创建仓库

1. 打开 https://github.com/new
2. Repository name 填：`PyTrace`
3. 选 **Public**（Public 的 Actions 打包不限时长，Private 也有每月 2000 分钟免费额度）
4. 不要勾任何初始化选项（README / .gitignore / license 都不勾）
5. 点绿色按钮 **Create repository**

### 2. 上传源码（网页拖拽，无需装 git）

1. 进入刚创建的空仓库页面，找到 **"uploading an existing file"** 链接点击
   （或点 **Add file → Upload files**）
2. 打开你电脑上的源码文件夹（G:\Pytrace\PyTrace），**全选里面所有文件和文件夹**
   （build 和 dist 这两个文件夹不用选，其他全要），**拖到网页中间的大框里**
3. 等右下角显示所有文件上传完毕（.github 文件夹要确认在列表里——没有的话单独拖它）
4. 底部 Commit changes 按钮直接点

### 3. 等云端打包（约 2~3 分钟）

1. 点仓库顶部的 **Actions** 标签
2. 会看到一条 "Build Windows exe" 正在跑（黄色圆点转圈）
3. 变成**绿色对勾** = 打包成功（红色叉 = 失败，点进去截图发我）

### 4. 下载 exe

1. 点那条成功记录，拉到页面最底部 **Artifacts** 区域
2. 下载 **PyTrace-Windows-exe**（一个 zip）
3. 解压出 **PyTrace.exe**——双击就是桌面 App，放桌面/任意位置都行

## 以后每次更新版本

我发你新 zip → 你在 GitHub 仓库里 **Add file → Upload files → 拖入新文件覆盖同名文件 → Commit** → Actions 自动重新打包 → 下载新 exe。全程 5 分钟，无需任何本地操作。

## 常见问题

| 现象 | 处理 |
|---|---|
| Actions 里没有工作流 | 确认 `.github\workflows\build.yml` 已上传（注意 .github 是隐藏式文件夹名，必须原样） |
| 打包失败红色叉 | 点进具体步骤看红字，截图发我 |
| Artifacts 下载的 zip 打不开 | Windows 解压时提示损坏 → 换浏览器重新下载 |
| exe 双击有 SmartScreen 蓝色警告 | 点「更多信息 → 仍要运行」（个人项目没有签名证书的正常现象） |
