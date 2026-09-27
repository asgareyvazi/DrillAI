/**
 * Route table.
 *
 * One list drives both `<Routes>` and the sidebar (see `NAV_ITEMS`), so a page cannot be reachable
 * without appearing in navigation. Every route is wrapped in an error boundary so a rendering
 * failure inside one workspace does not blank the application.
 */

import { Component, type ErrorInfo, type ReactNode } from 'react'
import { Navigate, Route, Routes } from 'react-router-dom'
import { AppShell } from './components/layout/AppShell'
import { Button, ErrorState } from './components/common'
import WellList from './pages/wells/WellList'
import WellCockpit from './pages/wells/WellCockpit'
import DocumentWorkspace from './pages/wells/DocumentWorkspace'
import EngineeringWorkspace from './pages/engineering/EngineeringWorkspace'
import OptimisationWorkspace from './pages/engineering/OptimisationWorkspace'
import AdvisorWorkspace from './pages/engineering/AdvisorWorkspace'
import ReportsWorkspace from './pages/engineering/ReportsWorkspace'
import WorkflowStudio from './pages/workflow/WorkflowStudio'
import RunMonitor from './pages/workflow/RunMonitor'
import LibraryPage from './pages/library/LibraryPage'
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
        <Route path="/wells/:wellId/cockpit" element={<PageErrorBoundary><WellCockpit /></PageErrorBoundary>} />
        <Route path="/wells/:wellId/documents" element={<PageErrorBoundary><DocumentWorkspace /></PageErrorBoundary>} />
        <Route path="/wells/:wellId/engineering" element={<PageErrorBoundary><EngineeringWorkspace /></PageErrorBoundary>} />
        <Route path="/wells/:wellId/optimisation" element={<PageErrorBoundary><OptimisationWorkspace /></PageErrorBoundary>} />
        <Route path="/wells/:wellId/advisor" element={<PageErrorBoundary><AdvisorWorkspace /></PageErrorBoundary>} />
        <Route path="/wells/:wellId/reports" element={<PageErrorBoundary><ReportsWorkspace /></PageErrorBoundary>} />
        <Route path="/workflows" element={<PageErrorBoundary><WorkflowStudio /></PageErrorBoundary>} />
        <Route path="/runs" element={<PageErrorBoundary><RunMonitor /></PageErrorBoundary>} />
        <Route path="/library" element={<PageErrorBoundary><LibraryPage /></PageErrorBoundary>} />
        <Route path="/platform" element={<PageErrorBoundary><PlatformPage /></PageErrorBoundary>} />
        <Route path="*" element={<NotFound />} />
      </Route>
    </Routes>
  )
}

export default App
