window.focusDesktop = {
  runtime: () => ({ apiBase: process.env.FOCUS_TASK_REAL_URL, session: 'focus-dev-session' }),
  selectWorkspace: async () => process.env.FOCUS_ASSEMBLY_PICK,
};
