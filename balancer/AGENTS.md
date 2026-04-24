<!-- Parent: ../AGENTS.md -->
<!-- Generated: 2026-04-24 | Updated: 2026-04-24 -->

# balancer

## Purpose
负载均衡器模块，实现优先级分组 + 加权轮询的负载均衡算法，以及渠道健康状态追踪和故障转移。

## Key Files
| File | Description |
|------|-------------|
| `__init__.py` | 模块初始化 (空文件) |
| `load_balancer.py` | LoadBalancer 类和 ChannelHealth 健康追踪，全局单例 load_balancer |

## Subdirectories
无

## For AI Agents

### Working In This Directory
- 修改负载均衡算法需注意并发安全
- 健康状态追踪使用内存存储，重启后丢失
- 冷却恢复时间和失败阈值在 config.py 中配置

### Testing Requirements
- 测试加权轮询算法的正确性
- 测试优先级分组选择逻辑
- 测试故障转移和渠道恢复逻辑
- 测试边界情况：所有渠道不可用

### Common Patterns
- 单例模式：全局 load_balancer 实例
- defaultdict 用于自动创建 ChannelHealth
- 平滑加权轮询算法 (Smooth Weighted Round-Robin)

## Dependencies

### Internal
- `models/channel.py` - Channel 模型
- `config.py` - MAX_FAIL_COUNT, COOLDOWN_SECONDS 配置

### External
无 (仅使用标准库)

<!-- MANUAL: -->
