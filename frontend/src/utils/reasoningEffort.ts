export const effortLabels = { low: '低', medium: '中', high: '高', xhigh: '超高', max: '最高' } as const
export type Effort = keyof typeof effortLabels
