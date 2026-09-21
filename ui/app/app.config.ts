export default defineAppConfig({
  ui: {
    colors: { primary: 'mint', neutral: 'zinc' },
    card: { slots: { root: 'bg-panel ring-line', body: 'p-4 sm:p-4' } },
    button: { slots: { base: 'rounded-full' } },
  },
})
