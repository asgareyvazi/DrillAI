/**
 * 404 page: a backend 404 and a frontend route miss are different, and both say what happened.
 *
 * The heading matters: a route miss has to be a page a screen reader and a keyboard user can
 * recognise, so it uses the same header as every other workspace instead of a bare card.
 */
import { Link } from 'react-router-dom'
import { useI18n } from '../i18n'
import { Button, Card } from '../components/common'
import { PageHeader } from '../components/layout/AppShell'

export default function NotFound() {
  const { t } = useI18n()
  return (
    <div className="space-y-4">
      <PageHeader
        title={t('errors.notFound')}
        description="No route matches this address. The navigation lists every workspace that exists."
        breadcrumb={<Link to="/wells">{t('nav.wells')}</Link>}
      />
      <Card title="404">
        <p className="text-sm text-graphite-600 dark:text-graphite-300">
          The address was not recognised by the application. This is not a data problem: nothing was
          requested from the backend, and no well was affected.
        </p>
        <p className="mt-1 text-xs text-graphite-500">
          If the address came from a link, the screen it pointed at may have moved. Every workspace the
          platform has is listed in the navigation.
        </p>
        <div className="mt-3 flex gap-2">
          <Link to="/wells">
            <Button size="sm" variant="primary">
              {t('nav.wells')}
            </Button>
          </Link>
          <Link to="/">
            <Button size="sm">Home</Button>
          </Link>
        </div>
      </Card>
    </div>
  )
}
