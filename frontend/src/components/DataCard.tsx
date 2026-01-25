import { ReactNode } from 'react'
import clsx from 'clsx'

interface DataCardProps {
  title: string
  icon: ReactNode
  color?: 'blue' | 'cyan' | 'indigo' | 'amber' | 'green' | 'red'
  children: ReactNode
}

const colorClasses = {
  blue: 'bg-blue-50 border-blue-200',
  cyan: 'bg-cyan-50 border-cyan-200',
  indigo: 'bg-indigo-50 border-indigo-200',
  amber: 'bg-amber-50 border-amber-200',
  green: 'bg-green-50 border-green-200',
  red: 'bg-red-50 border-red-200',
}

const iconColorClasses = {
  blue: 'text-blue-600 bg-blue-100',
  cyan: 'text-cyan-600 bg-cyan-100',
  indigo: 'text-indigo-600 bg-indigo-100',
  amber: 'text-amber-600 bg-amber-100',
  green: 'text-green-600 bg-green-100',
  red: 'text-red-600 bg-red-100',
}

export default function DataCard({
  title,
  icon,
  color = 'blue',
  children,
}: DataCardProps) {
  return (
    <div
      className={clsx(
        'rounded-lg border p-4',
        colorClasses[color]
      )}
    >
      <div className="flex items-center gap-3 mb-3">
        <div
          className={clsx(
            'p-2 rounded-lg',
            iconColorClasses[color]
          )}
        >
          {icon}
        </div>
        <h3 className="font-medium text-gray-700">{title}</h3>
      </div>
      <div>{children}</div>
    </div>
  )
}
