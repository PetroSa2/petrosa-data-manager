import { NavLink, Outlet } from "react-router-dom";
import { useOperator } from "./lib/operator";

const navLinkClass = ({ isActive }: { isActive: boolean }) =>
  [
    "px-3 py-1.5 rounded-md text-sm font-medium transition-colors",
    isActive
      ? "bg-slate-800 text-slate-100"
      : "text-slate-400 hover:text-slate-100 hover:bg-slate-900",
  ].join(" ");

export default function App() {
  const operator = useOperator();

  return (
    <div className="min-h-screen bg-slate-950 text-slate-100 font-mono">
      <header className="border-b border-slate-800">
        <div className="mx-auto max-w-6xl px-4 py-3 flex items-center justify-between">
          <div className="flex items-center gap-6">
            <span className="text-sm font-semibold tracking-wide text-slate-200">
              petrosa.operator
            </span>
            <nav className="flex items-center gap-1">
              <NavLink to="/" end className={navLinkClass}>
                home
              </NavLink>
              <NavLink to="/time/now" className={navLinkClass}>
                time
              </NavLink>
              <NavLink to="/strategies" className={navLinkClass}>
                strategies
              </NavLink>
            </nav>
          </div>
          <div className="text-xs text-slate-500">
            {operator ? (
              <span>
                operator: <span className="text-slate-300">{operator}</span>
              </span>
            ) : (
              <span className="text-amber-400">no operator header</span>
            )}
          </div>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-8">
        <Outlet />
      </main>
    </div>
  );
}
