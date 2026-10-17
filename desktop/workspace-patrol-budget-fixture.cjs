/* 离线 UI 测试预算目录；通过合同测试与服务端产品定义核对。 */
module.exports = { default: "normal", presets: {
  normal: { label: "普通任务", budgets: { max_rounds: 40, max_duration_seconds: 1800, max_model_calls: 100, max_input_tokens: 500000, max_output_tokens: 50000, max_retries: 6, max_concurrent_runs: 2 } },
  long: { label: "长任务", budgets: { max_rounds: 160, max_duration_seconds: 7200, max_model_calls: 400, max_input_tokens: 2000000, max_output_tokens: 200000, max_retries: 20, max_concurrent_runs: 4 } },
} };
