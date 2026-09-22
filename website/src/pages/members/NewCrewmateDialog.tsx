/**
 * "New crewmate" — the create dialog the Crewmates page opens from its header
 * "+" and its empty-state hero.
 *
 * A crewmate IS a crew record, so this posts to the same `POST /api/agents`
 * the crew manager's create form uses; the two stay one write path with two
 * front doors. The difference is what the user is asked first: a name, what
 * it is built from, and — in plain words — what it looks after. Everything
 * the crew manager's form also asks (workspace, model, routing triggers,
 * session colour) sits behind an "Advanced" disclosure, rendered by the SAME
 * `Field` frame and field components the editor mounts, so the two forms
 * cannot drift.
 *
 * "Built from" lists the installed kiro agents (the templates a crew can
 * boot), never the configured default CREW: a crew named `default` is an
 * alias, and storing its name as `kiro_agent` would make the new crewmate run
 * a fallback instead of that crew's template. The built-in `kirocrew` agent
 * leads the list and is labelled as the default.
 *
 * "What it looks after" is stored as the crew record's `description`: the
 * one free-text field the record already carries for a human-readable
 * account of the crew, and the line the crewmate's first greeting is seeded
 * from (see MembersPage). Memory is provisioned by the server on create
 * (a private store per crewmate, never a choice here), and the avatar is
 * edited on the detail page afterwards — the same split the editor's create
 * form has.
 *
 * Kept mounted and driven by `open` (Modal's own contract): `Modal` renders
 * nothing while closed, and the form state below is reset on every open so a
 * dismissed draft does not reappear.
 */
import { useEffect, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { ChevronRight } from 'lucide-react'
import { useMutation, useQuery } from '@tanstack/react-query'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'

import Modal from '../../components/Modal'
import SimpleSelect from '../../components/SimpleSelect'
import ErrorNotice from '../../components/ErrorNotice'
import { Btn, Input } from '../../components/ui'
import { api } from '../../api/client'
// From the side-effect-free module, not `api/client`: test doubles of the
// client mock only `api`, and an `instanceof` against an undefined import
// throws instead of falling through to the generic message.
import { ApiError } from '../../api/apiError'
import { parseErrorCode } from '../../utils/errorReport'
import { useAvailableModelsQuery } from '../../hooks/useAvailableModels'
import {
  Field,
  INHERIT_MODEL,
  ModelField,
  SessionColorField,
  TriggersField,
  WorkspaceField,
  WorkspaceModal,
} from '../KiroCrewAgentsPage'

/** The built-in kiro agent every install ships; the list's default entry. */
const BUILTIN_AGENT = 'kirocrew'

/** What the page needs to open the new crewmate's chat and seed its greeting. */
export interface CreatedCrewmate {
  /** Exact crew name — MembersPage's `?member=` resolves by name. */
  name: string
  /** The "what it looks after" line as typed; '' when left blank. */
  job: string
}

/** The `POST /api/agents` body — the crew manager's create payload plus the
 *  record's `description`, which carries "what it looks after". `model` is
 *  sent only when pinned: the inherit spelling is the server's default. */
interface CreateBody {
  name: string
  kiro_agent: string
  workspace: string
  memory_store: string
  description: string
  triggers: string
  session_color: string
  model?: string
}

export default function NewCrewmateDialog({ open, onClose, onCreated }: {
  open: boolean
  onClose: () => void
  /** Fired once the server has the record; the page takes it from there. */
  onCreated: (created: CreatedCrewmate) => void
}) {
  const { t } = useTranslation()
  const reduceMotion = useReducedMotion()

  const [name, setName] = useState('')
  const [builtFrom, setBuiltFrom] = useState('')
  const [job, setJob] = useState('')
  const [advanced, setAdvanced] = useState(false)
  const [workspace, setWorkspace] = useState('default')
  const [model, setModel] = useState(INHERIT_MODEL)
  const [triggers, setTriggers] = useState('')
  const [sessionColor, setSessionColor] = useState('')
  const [wsModalOpen, setWsModalOpen] = useState(false)
  // Client-side validation ("name is required") is kept apart from a request
  // that FAILED: a blank name never left the browser, so it is not an error.
  const [hint, setHint] = useState('')
  const [error, setError] = useState('')

  // Every open starts blank: a dismissed draft must not come back.
  useEffect(() => {
    if (!open) return
    setName(''); setBuiltFrom(''); setJob(''); setAdvanced(false)
    setWorkspace('default'); setModel(INHERIT_MODEL); setTriggers(''); setSessionColor('')
    setHint(''); setError('')
  }, [open])

  // Option lists come from the same reads the crew editor uses, fetched only
  // while the dialog is open. Each falls back to its built-in default when
  // the read fails, and the failure is SAID: one notice, first failure wins,
  // so a shortened list never passes for the whole set of choices.
  const { data: installedAgents, error: installedError } = useQuery({
    queryKey: ['agents-installed'],
    queryFn: () => api.agentsInstalled(),
    enabled: open,
  })
  const { data: workspacesData, refetch: refetchWorkspaces, error: workspacesError } = useQuery({
    queryKey: ['workspaces'],
    queryFn: () => api.workspaces(),
    enabled: open,
  })
  const { data: availableModels, error: modelsError } = useAvailableModelsQuery({ enabled: open })
  const optionsError = installedError ?? workspacesError ?? modelsError

  // Installed kiro agents only (see the header comment). Private fork copies
  // (one crew's own definition) are not offered — a copy named after crew A
  // means nothing in crew B's list. The built-in agent leads, labelled as the
  // default; it is offered even when the installed read failed, because it
  // ships with every install.
  const installed = Array.isArray(installedAgents)
    ? installedAgents
      .filter((x: { name: string; private_to?: string }) => Boolean(x.name) && !x.private_to)
      .map((x: { name: string }) => x.name)
      .filter((n: string) => n !== BUILTIN_AGENT)
    : []
  const builtFromOptions = [BUILTIN_AGENT, ...installed]
  const builtFromLabels = builtFromOptions.map((n) =>
    n === BUILTIN_AGENT ? t('pages.membersPage.built_from_default', { agent: n }) : n,
  )
  const builtFromValue = builtFrom || BUILTIN_AGENT
  const workspaceOptions = workspacesData?.workspaces?.map((w: { name: string }) => w.name) || ['default']
  const modelOptions = [
    INHERIT_MODEL,
    ...(availableModels || []).map((m) => m.name).filter((n) => n && n !== INHERIT_MODEL),
  ]

  // Every editable value counts as a draft, not only the two text fields: a
  // template or an Advanced pick is as lost on an accidental dismissal as a
  // typed name, and the reset-on-open above means there is no way back.
  const dirty = Boolean(
    name || job || builtFrom || workspace !== 'default' || model !== INHERIT_MODEL || triggers || sessionColor,
  )

  const createMut = useMutation({
    mutationFn: (body: CreateBody) => api.createKirocrewAgent(body) as Promise<{ error?: string }>,
    onSuccess: (r, body) => {
      if (r?.error) { setError(r.error); return }
      onCreated({ name: body.name, job: body.description })
    },
    onError: (e: Error, body) => {
      if (e instanceof ApiError && e.status === 409 && parseErrorCode(e.body) === 'agent_exists') {
        setError(t('pages.membersPage.create_name_taken', { name: body.name }))
        return
      }
      setError(e.message || t('pages.membersPage.create_failed'))
    },
  })
  const busy = createMut.isPending

  const submit = () => {
    setError(''); setHint('')
    const n = name.trim()
    if (!n) { setHint(t('pages.membersPage.create_name_required')); return }
    createMut.mutate({
      name: n,
      kiro_agent: builtFromValue,
      workspace,
      memory_store: 'default',
      description: job.trim(),
      triggers,
      session_color: sessionColor,
      ...(model !== INHERIT_MODEL ? { model } : {}),
    })
  }

  return (
    <>
      <Modal
        open={open}
        onClose={onClose}
        title={t('pages.membersPage.add_member')}
        maxWidth={480}
        guardAccidentalDismiss={dirty}
        dismissDisabled={busy}
        footer={
          <>
            <Btn onClick={onClose} disabled={busy}>{t('pages.membersPage.create_cancel')}</Btn>
            <Btn primary onClick={submit} disabled={busy} data-testid="crewmate-create-submit">
              {busy ? t('pages.membersPage.create_submitting') : t('pages.membersPage.create_submit')}
            </Btn>
          </>
        }
      >
        <form
          className="flex flex-col gap-5"
          data-testid="crewmate-create-form"
          onSubmit={(e) => { e.preventDefault(); if (!busy) submit() }}
        >
          <Field label={t('pages.membersPage.create_name')} hint={hint || undefined}>
            <Input
              value={name}
              onChange={(e) => { setName(e.target.value); setHint(''); setError('') }}
              aria-label={t('pages.membersPage.create_name')}
              aria-invalid={hint ? true : undefined}
              placeholder={t('pages.membersPage.create_name_placeholder')}
              autoFocus
              disabled={busy}
            />
          </Field>
          <Field label={t('pages.membersPage.agent_template')} hint={t('pages.membersPage.built_from_hint')}>
            <SimpleSelect
              options={builtFromOptions}
              optionLabels={builtFromLabels}
              value={builtFromValue}
              onChange={setBuiltFrom}
              disabled={busy}
              aria-label={t('pages.membersPage.agent_template')}
            />
          </Field>
          <Field
            label={`${t('pages.membersPage.create_job')} · ${t('pages.membersPage.create_optional')}`}
            hint={t('pages.membersPage.create_job_hint')}
          >
            <Input
              value={job}
              onChange={(e) => setJob(e.target.value)}
              aria-label={t('pages.membersPage.create_job')}
              placeholder={t('pages.membersPage.create_job_placeholder')}
              disabled={busy}
            />
          </Field>
          <div className="flex flex-col gap-4">
            <button
              type="button"
              onClick={() => setAdvanced((v) => !v)}
              aria-expanded={advanced}
              aria-controls="crewmate-create-advanced"
              className="flex items-center gap-1 self-start -ml-1 px-1 py-0.5 rounded text-[12px] text-muted hover:text-text bg-transparent border-none cursor-pointer focus-ring"
              data-testid="crewmate-create-advanced-toggle"
            >
              <ChevronRight
                size={13}
                className={`lucide-inline transition-transform duration-150 motion-reduce:transition-none ${advanced ? 'rotate-90' : ''}`}
                aria-hidden="true"
              />
              {t('pages.membersPage.create_advanced')}
            </button>
            {/* The disclosure grows out of its toggle instead of appearing whole:
                the same element, unfolding — so the reader sees where the extra
                fields came from. Cut, not animated, under reduced motion. */}
            <AnimatePresence initial={false}>
              {advanced && (
                <motion.div
                  key="advanced"
                  id="crewmate-create-advanced"
                  className="flex flex-col gap-4 overflow-hidden"
                  initial={reduceMotion ? false : { height: 0, opacity: 0 }}
                  animate={{ height: 'auto', opacity: 1 }}
                  exit={reduceMotion ? { opacity: 0, transition: { duration: 0 } } : { height: 0, opacity: 0 }}
                  transition={{ duration: 0.18, ease: 'easeOut' }}
                  data-testid="crewmate-create-advanced"
                >
                  <WorkspaceField
                    subject="member"
                    hint={t('pages.membersPage.create_workspace_hint')}
                    options={workspaceOptions}
                    value={workspace}
                    onChange={setWorkspace}
                    onNewWorkspace={() => setWsModalOpen(true)}
                  />
                  <ModelField options={modelOptions} value={model} onChange={setModel} />
                  <TriggersField value={triggers} onChange={setTriggers} subject="member" />
                  <SessionColorField value={sessionColor} onChange={setSessionColor} subject="member" />
                </motion.div>
              )}
            </AnimatePresence>
          </div>
          {/* No hand-off on either notice: both sit over this unsaved form —
              the name, job and every Advanced pick live only in local state —
              and the hand-off navigates to the chat, unmounting the dialog
              and the draft with it. */}
          {optionsError && !error && (
            <ErrorNotice message={t('pages.membersPage.create_options_failed')} testId="crewmate-create-options-error" />
          )}
          {error && <ErrorNotice message={error} testId="crewmate-create-error" />}
        </form>
      </Modal>
      <WorkspaceModal
        open={wsModalOpen}
        workspaceOptions={workspaceOptions}
        onCreated={(newName) => { setWsModalOpen(false); refetchWorkspaces().then(() => setWorkspace(newName)) }}
        onClose={() => setWsModalOpen(false)}
      />
    </>
  )
}
