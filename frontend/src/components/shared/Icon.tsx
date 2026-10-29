import type { CSSProperties, MouseEventHandler } from 'react';
import { lucideIconMap } from './lucideIcons';
import s from './Icon.module.css';

interface IconProps {
  name: string;
  size?: number;
  strokeWidth?: number;
  style?: CSSProperties;
  className?: string;
  onClick?: MouseEventHandler<HTMLSpanElement>;
}

/**
 * Icon component wrapping lucide-react.
 * Drop-in replacement for the old CDN-based lucide icon component.
 *
 * Usage: <Icon name="Search" size={20} />
 */
export const Icon = ({ name, size = 20, strokeWidth = 1.5, style = {}, className = '', onClick }: IconProps) => {
  // lucide-react exports PascalCase components, e.g. icons.Search, icons.FileText
  // The old code used kebab-case names like "search", "file-text"
  // Convert kebab-case to PascalCase
  const pascalName = name
    .split('-')
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join('');

  const LucideIcon = lucideIconMap[pascalName];

  if (!LucideIcon) {
    if (import.meta.env.DEV) {
      console.warn(`Icon "${name}" is not in lucideIconMap (components/shared/lucideIcons.ts); it renders empty.`);
    }
    // Fallback: render empty span if icon not found
    return <span className={`${s.icon} ${className}`} style={{ width: size, height: size, ...style }} />;
  }

  return (
    <span
      className={`${s.icon} ${onClick ? s.iconClickable : ''} ${className}`}
      onClick={onClick}
      style={style}
    >
      <LucideIcon size={size} strokeWidth={strokeWidth} />
    </span>
  );
};
