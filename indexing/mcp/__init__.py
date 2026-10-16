"""
MCP 服务集合

按能力拆成独立服务，各自独立端口与密钥：
- piece-kb（retrieval/server.py）：文档检索、原页与精确引文，只读
- piece-index（indexing/mcp/server.py）：文件、卡片、集合与索引任务
- piece-graph（indexing/mcp/graph_server.py）：图谱实体、语义关系与证据
- piece-wiki（indexing/mcp/wiki_server.py）：Markdown 知识页与引用
- piece-zotero（indexing/mcp/zotero_server.py）：本机 Zotero 查阅、导入与有限写入

图谱与 Wiki 读写同服务（是否允许写入由用户选择接入哪个服务决定）；
read 与 write 共用 shared.py 里的 params 包装与意图式写入辅助。
"""
