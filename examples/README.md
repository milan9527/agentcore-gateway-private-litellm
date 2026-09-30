# 脱敏实测证据

本目录来自 2026-09-30 东京区域的真实部署测试。普通模型推理、SSE、API_KEY 注入和 Gateway → LiteLLM 私网路径均通过。

发布时做了以下处理：

- AWS 账号、域名和资源 ID 使用一致的示例值替换；这些标识不能用于连接原部署。
- 省略部分 AWS 响应元数据、响应头和模型 reasoning 字段。
- 保留请求标识、测试文本、HTTP 状态、后端 IP 和 ENI 的对应关系。
- 不包含 GitHub token、LiteLLM key、AWS 凭证或活动部署状态。

这些是经过整理的历史证据，不应当作新部署的配置。新部署请修改仓库根目录的配置示例，并运行部署脚本。

## 查看或重放

```bash
python replay_evidence.py
```

在仓库根目录运行。脚本在临时目录中对以下信息执行关联断言，不调用 AWS，也不修改当前部署：

1. EC2 没有公网 IP，ALB 为 internal。
2. ALB 和 EC2 入站规则仅允许预期的上一级安全组。
3. 三次成功请求的 `x-probe-id` 与后端日志一致。
4. 日志里的 `forwarded_for` 匹配 Lattice ENI 私有 IP。
5. 普通推理有模型输出，SSE 有内容和 `[DONE]`。

## 文件索引

| 文件 | 用途 |
|---|---|
| [observed-resources.json](observed-resources.json) | 重放时使用的脱敏资源标识 |
| [verified-private-path.json](evidence/verified-private-path.json) | 核对后的三次请求及网络路径 |
| [create-target-corrected-request.json](evidence/create-target-corrected-request.json) | 成功创建 inference target 的请求 |
| [gateway-policy.json](evidence/gateway-policy.json) | 实际生效权限的脱敏副本 |
| [inference-bed48286cef1.json](evidence/inference-bed48286cef1.json) | 真实模型请求和响应 |
| [stream-f8534fef0943.json](evidence/stream-f8534fef0943.json) | SSE 请求和响应 |
| [访问日志](evidence/access-proof-inference-26599f4e-9330-4cad-84c5-9b9f7d0e2365.json) | 请求与来源 IP 的对应关系 |
| [Lattice ENI](evidence/proof-lattice-network-interfaces.json) | ENI 私有地址 |
| [安全组](evidence/proof-security-groups.json) | 限定来源的入站规则 |

`observed-resources.json` 不应复制为 `private-litellm-state.json`。实际部署会生成自己的状态文件。
