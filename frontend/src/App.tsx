/**
 * Route table.
 *
 * One list drives both `<Routes>` and the sidebar (see `NAV_ITEMS`), so a page cannot be reachable
 * without appearing in navigation. Every route is wrapped in an error boundary so a rendering
 * failure inside one workspace does not blank the application.
 */

import { Component, type ErrorInfo, type ReactNode } from 'react'
import { Navigate, Route, Routes, useLocation, useParams } from 'react-router-dom'
import { AppShell } from './components/layout/AppShell'
import { Button, ErrorState } from './components/common'
import WellList from './pages/wells/WellList'
import MasterDataWorkspace from './pages/master-data/MasterDataWorkspace'
import WellCockpit from './pages/wells/WellCockpit'
import DocumentWorkspace from './pages/wells/DocumentWorkspace'
import OperationsWorkspace from './pages/wells/OperationsWorkspace'
import EngineeringWorkspace from './pages/engineering/EngineeringWorkspace'
import OptimisationWorkspace from './pages/engineering/OptimisationWorkspace'
import AdvisorWorkspace from './pages/engineering/AdvisorWorkspace'
import ReportsWorkspace from './pages/engineering/ReportsWorkspace'
import WorkflowStudio from './pages/workflow/WorkflowStudio'
import RunMonitor from './pages/workflow/RunMonitor'
import LibraryPage from './pages/library/LibraryPage'
import ConnectorsPage from './pages/library/ConnectorsPage'
import PlatformPage from './pages/library/PlatformPage'
import NotFound from './pages/NotFound'

class PageErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Surfaced in the browser console for a developer; the user sees a recoverable panel rather
    // than a blank screen. No engineering value is inferred from a failed render.
    console.error('workspace render failed', error, info.componentStack)
  }

  render() {
    if (this.state.error) {
      return (
        <div className="space-y-3">
          <ErrorState error={this.state.error} />
          <Button onClick={() => this.setState({ error: null })}>Try again</Button>
        </div>
      )
    }
    return this.props.children
  }
}

/**
 * A page whose whole subject is one well.
 *
 * The key is the well, so a change of context in the URL *replaces* the page instead of re-rendering
 * it. Without that, everything a page holds in its own state survives the change — the section that
 * was selected, the report that was generated, the advisor answer that was read — and the next well's
 * screen shows the previous well's engineering as if it belonged to it. The rule this states is the
 * one a reader would assume: a page is about exactly the well in its URL, and nothing about another
 * well can be on it.
 *
 * The error boundary is inside the key too, so a well whose page failed does not stay failed after the
 * reader moves to another well.
 */
function WellScoped({ children }: { children: ReactNode }) {
  const { wellId } = useParams<{ wellId: string }>()
  const { pathname } = useLocation()
  // The path is the key: a different well, or a different workspace within the same well, is a
  // different subject. Search parameters are deliberately *not* part of it — `?document=`, `?tab=` and
  // the rest are navigations *within* one page, and remounting for those would discard the reader's
  // work and their scroll position for no reason.
  return <PageErrorBoundary key={`${wellId}${pathname}`}>{children}</PageErrorBoundary>
}

export function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route index element={<Navigate to="/wells" replace />} />
        <Route
          path="/wells"
          element={
            <PageErrorBoundary>
              <WellList />
            </PageErrorBoundary>
          }
        />
        <Route
          path="/master-data"
          element={
            <PageErrorBoundary>
              <MasterDataWorkspace />
            </PageErrorBoundary>
          }
        />
        <Route path="/wells/:wellId/cockpit" element={<WellScoped><WellCockpit /></WellScoped>} />
        <Route path="/wells/:wellId/documents" element={<WellScoped><DocumentWorkspace /></WellScoped>} />
        <Route path="/wells/:wellId/operations" element={<WellScoped><OperationsWorkspace /></WellScoped>} />
        <Route path="/wells/:wellId/engineering" element={<WellScoped><EngineeringWorkspace /></WellScoped>} />
        <Route path="/wells/:wellId/optimisation" element={<WellScoped><OptimisationWorkspace /></WellScoped>} />
        <Route path="/wells/:wellId/advisor" element={<WellScoped><AdvisorWorkspace /></WellScoped>} />
        <Route path="/wells/:wellId/reports" element={<WellScoped><ReportsWorkspace /></WellScoped>} />
        <Route path="/workflows" element={<PageErrorBoundary><WorkflowStudio /></PageErrorBoundary>} />
        <Route path="/runs" element={<PageErrorBoundary><RunMonitor /></PageErrorBoundary>} />
        <Route path="/library" element={<PageErrorBoundary><LibraryPage /></PageErrorBoundary>} />
        <Route path="/connectors" element={<PageErrorBoundary><ConnectorsPage /></PageErrorBoundary>} />
        <Route path="/platform" element={<PageErrorBoundary><PlatformPage /></PageErrorBoundary>} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  )
}

export default App
