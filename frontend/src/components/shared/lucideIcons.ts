import type { LucideIcon } from 'lucide-react';
import {
  AlertCircle, AlertTriangle, ArrowLeft, ArrowRight, ArrowUp, Award,
  BookOpen, Box, Briefcase, Building,
  Calendar, CalendarClock, CalendarX, Check, CheckCircle,
  ChevronDown, ChevronLeft, ChevronRight, ChevronUp, ChevronsDown,
  Clock, Cloud, Columns2, Compass, Copy, Crosshair,
  Database, Download, Droplet,
  Eraser, ExternalLink, Eye, EyeOff,
  File, FileEdit, FilePen, FileSearch, FileStack, FileText, Files,
  Folder, FolderOpen, FolderPlus, FolderSearch, FolderTree, FolderX,
  Gavel, GitCompare, GripHorizontal,
  HardDrive, Hash, HelpCircle, History,
  Info, Key, KeyRound, Layers, Lightbulb, Link, ListOrdered,
  Loader, Loader2, Lock, LogIn, LogOut,
  Mail, Maximize2, Menu, MessageSquare, MessageSquarePlus, MessagesSquare, Mic, Minus,
  PanelRight, Paperclip, PenLine, Pencil, Plus, Quote,
  RefreshCw, RotateCcw,
  Save, Scale, ScanSearch, Search, SearchX, Send, Server, Settings,
  Shield, ShieldAlert, ShieldCheck, ShieldOff, Sparkles,
  Play,
  Table2, Target, ThumbsDown, ThumbsUp, Trash2, TrendingDown, TrendingUp,
  Upload, User, UserPlus,
  Wand2, Workflow, Wrench,
  X, XCircle, Zap,
  Activity, CheckCircle2, FileCheck, FileX, FolderInput, FolderKanban, MonitorSmartphone, Text, UserMinus, Users,
  ArrowDown, ArrowUpDown, ClipboardCheck, FileJson, Filter, Flag, FlagOff, GripVertical, LayoutTemplate,
  ListFilter, MoreHorizontal, PanelRightClose, Rows3, Settings2, SlidersHorizontal, Undo2,
  Archive, ArchiveRestore, CircleSlash, FileDown, GitBranch, Hourglass, ListChecks, RotateCw, SkipForward, UserCheck,
} from 'lucide-react';

/**
 * Explicit map of every lucide icon the app uses (PascalCase names).
 *
 * Importing named icons — instead of `import * as icons` — lets Rollup
 * tree-shake the icon library down to only these icons (the full set is the
 * single largest module in the bundle). When you use a NEW icon name in an
 * `<Icon name="..." />`, add it to the import and to this map, or the icon
 * renders as an empty span.
 */
export const lucideIconMap: Record<string, LucideIcon> = {
  AlertCircle, AlertTriangle, ArrowLeft, ArrowRight, ArrowUp, Award,
  BookOpen, Box, Briefcase, Building,
  Calendar, CalendarClock, CalendarX, Check, CheckCircle,
  ChevronDown, ChevronLeft, ChevronRight, ChevronUp, ChevronsDown,
  Clock, Cloud, Columns2, Compass, Copy, Crosshair,
  Database, Download, Droplet,
  Eraser, ExternalLink, Eye, EyeOff,
  File, FileEdit, FilePen, FileSearch, FileStack, FileText, Files,
  Folder, FolderOpen, FolderPlus, FolderSearch, FolderTree, FolderX,
  Gavel, GitCompare, GripHorizontal,
  HardDrive, Hash, HelpCircle, History,
  Info, Key, KeyRound, Layers, Lightbulb, Link, ListOrdered,
  Loader, Loader2, Lock, LogIn, LogOut,
  Mail, Maximize2, Menu, MessageSquare, MessageSquarePlus, MessagesSquare, Mic, Minus,
  PanelRight, Paperclip, PenLine, Pencil, Plus, Quote,
  RefreshCw, RotateCcw,
  Save, Scale, ScanSearch, Search, SearchX, Send, Server, Settings,
  Shield, ShieldAlert, ShieldCheck, ShieldOff, Sparkles,
  Play,
  Table2, Target, ThumbsDown, ThumbsUp, Trash2, TrendingDown, TrendingUp,
  Upload, User, UserPlus,
  Wand2, Workflow, Wrench,
  X, XCircle, Zap,
  Activity, CheckCircle2, FileCheck, FileX, FolderInput, FolderKanban, MonitorSmartphone, Text, UserMinus, Users,
  ArrowDown, ArrowUpDown, ClipboardCheck, FileJson, Filter, Flag, FlagOff, GripVertical, LayoutTemplate,
  ListFilter, MoreHorizontal, PanelRightClose, Rows3, Settings2, SlidersHorizontal, Undo2,
  Archive, ArchiveRestore, CircleSlash, FileDown, GitBranch, Hourglass, ListChecks, RotateCw, SkipForward, UserCheck,
};
