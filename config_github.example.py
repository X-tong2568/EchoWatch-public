# config_github.example.py
# EchoWatch GitHub 互动归档配置示例
# 使用方法：复制为 config_github.py，填入 token；config_github.py 已在 .gitignore

# 是否启用归档
GITHUB_ARCHIVE_ENABLED = False

# 与通知归档共用的私有归档仓
GITHUB_ARCHIVE_OWNER = "your-github-username"
GITHUB_ARCHIVE_REPO = "your-archive-repo"
GITHUB_ARCHIVE_BRANCH = "main"

# fine-grained PAT：只授该私有仓 Contents: Read and write
# 不要复用通知系统的 token
GITHUB_ARCHIVE_TOKEN = ""

# 请求超时（秒）
GITHUB_ARCHIVE_TIMEOUT = 20

# True=只写 data/archive_preview/，不提交 GitHub；默认 True
GITHUB_ARCHIVE_DRY_RUN = True

# 图片最长边与 WebP 质量
GITHUB_ARCHIVE_IMAGE_MAX_SIZE = 1280
GITHUB_ARCHIVE_WEBP_QUALITY = 80

# EchoWatch 专属可选项
ARCHIVE_ANONYMIZE_NICK = False       # True=昵称哈希成 粉丝#abcd
ARCHIVE_NEVER_ARCHIVE_PINNED = True  # 冻结历史置顶，防 L0 级联清理
ARCHIVE_FLUSH_SECONDS = 180          # flush 间隔（秒）

# 归档提交署名（可选）
# 默认 "EchoWatch Archive <archive@local>"，不关联任何 GitHub 账号；
# 想计入个人贡献图，填入 GitHub 账号已验证的用户名和邮箱
# GITHUB_ARCHIVE_AUTHOR_NAME = "your-github-username"
# GITHUB_ARCHIVE_AUTHOR_EMAIL = "your-verified-email@example.com"
