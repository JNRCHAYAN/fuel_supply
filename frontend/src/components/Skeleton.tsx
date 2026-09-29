export function Skeleton({
  variant = 'line', count = 1,
}: { variant?: 'line' | 'block' | 'tile'; count?: number }) {
  const height = variant === 'tile' ? 'h-20' : variant === 'block' ? 'h-32' : 'h-4'
  return (
    <div role="status" aria-label="Loading" className="flex flex-col gap-2">
      {Array.from({ length: count }, (_, i) => (
        <div
          key={i}
          className={`${height} w-full animate-pulse rounded-control bg-surface-raised motion-reduce:animate-none`}
          style={variant === 'line' && i === count - 1 ? { width: '60%' } : undefined}
        />
      ))}
    </div>
  )
}
