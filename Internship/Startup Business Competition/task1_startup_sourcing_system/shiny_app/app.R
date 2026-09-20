# ============================================================================
# University Startup Sourcing Agent — R Shiny front-end
# ----------------------------------------------------------------------------
# The pipeline/LLM/crawling/e-mail logic is Python (startup_sourcing/*). This R
# Shiny app is the UI: it displays data natively and shells out to small Python
# helper scripts (shiny_app/py/*) via processx to run the pipeline and send mail.
# ============================================================================
library(shiny)
library(bslib)
library(DT)
library(readr)
library(dplyr)
library(jsonlite)
library(processx)

# --- Paths -------------------------------------------------------------------
PROJECT <- normalizePath(file.path(getwd(), ".."), winslash = "/", mustWork = FALSE)  # shiny_app/.. = pipeline root (survives folder renames)
APPDIR  <- getwd()                          # shiny_app/ — the only dir bundled by rsconnect
BUNDLED <- file.path(APPDIR, "bundled")     # seeds + sample output shipped with the app
PYDIR   <- file.path(PROJECT, "shiny_app", "py")
PY      <- "python"   # on PATH
# Local dev ships the full Python pipeline as a sibling and has Python on PATH.
# The hosted (shinyapps.io) app has neither, so it becomes a viewer + outreach
# tool: CAN_RUN gates the pipeline / contact-waterfall features.
CAN_RUN <- dir.exists(file.path(PROJECT, "startup_sourcing")) && nzchar(Sys.which(PY))
DATA    <- if (dir.exists(file.path(PROJECT, "data")))    file.path(PROJECT, "data")    else file.path(BUNDLED, "data")
OUTROOT <- if (dir.exists(file.path(PROJECT, "outputs"))) file.path(PROJECT, "outputs") else file.path(BUNDLED, "outputs")
RUN_OUT <- file.path(OUTROOT, "shiny_run")

# --- One-time settings info from Python (competition text, SMTP status) -------
INFO_DEFAULT <- list(
  competition_name = "13th Lee Kuan Yew Global Business Plan Competition", competition_year = 2027,
  organizer_name = "", organizer_signature = "SMU Institute of Innovation & Entrepreneurship",
  competition_url = "https://lkygbpc.smu.edu.sg/", allow_live_send = FALSE, smtp_user = "", smtp_host = "")
INFO <- if (CAN_RUN) tryCatch(
  jsonlite::fromJSON(paste(system2(PY, c(shQuote(file.path(PYDIR, "info.py")), shQuote(PROJECT)),
                                   stdout = TRUE, stderr = FALSE), collapse = "")),
  error = function(e) INFO_DEFAULT) else INFO_DEFAULT

DEFAULT_SUBJECT <- sprintf("Invitation to the %s - {startup_name}", INFO$competition_name)
DEFAULT_BODY <- paste(
  "Dear {founder_name},", "",
  sprintf("I'm writing from the Institute of Innovation & Entrepreneurship at Singapore Management University. We've been following {startup_name}'s work in {industry}, and we'd like to invite you to take part in the %s (%s).",
          INFO$competition_name, INFO$competition_year),
  "", "Would you be open to a short conversation about applying?", "",
  "Warm regards,", INFO$organizer_signature, sep = "\n")

# --- Helpers -----------------------------------------------------------------
seed_files <- function() {
  fs <- list.files(DATA, pattern = "\\.csv$", full.names = TRUE)
  keep <- fs[vapply(fs, function(f) {
    h <- tryCatch(readLines(f, n = 1, warn = FALSE), error = function(e) "")
    grepl("Seed URL", h)
  }, logical(1))]
  bn <- basename(keep)
  if (!"seed_sources.csv" %in% bn && file.exists(file.path(DATA, "seed_sources.csv")))
    bn <- c("seed_sources.csv", bn)
  sort(unique(bn))
}

read_seed <- function(name) {
  suppressWarnings(readr::read_csv(file.path(DATA, name), show_col_types = FALSE,
                                   col_types = readr::cols(.default = "c")))
}

output_dirs <- function() {
  dirs <- list.dirs(OUTROOT, recursive = TRUE)
  keep <- dirs[vapply(dirs, function(d)
    any(file.exists(file.path(d, c("candidates_ranked.csv", "candidates_enriched.csv", "founders.csv")))),
    logical(1))]
  setNames(keep, sub(paste0("^", OUTROOT, "/?"), "", keep))
}

read_out <- function(dir, names) {
  for (n in names) {
    p <- file.path(dir, n)
    if (file.exists(p)) return(suppressWarnings(readr::read_csv(p, show_col_types = FALSE,
                                                               col_types = readr::cols(.default = "c"))))
  }
  NULL
}

first_col <- function(df, candidates) { for (c in candidates) if (c %in% names(df)) return(df[[c]]); rep("", nrow(df)) }

# Interactive table styled like the project website: per-column filter row,
# compact striped/hover rows, left-aligned cells; navy header comes from app_css.
web_dt <- function(df, page = 12, selection = "none", escape = TRUE, extra_defs = NULL) {
  defs <- c(extra_defs, list(list(className = "dt-left", targets = "_all")))
  DT::datatable(df, rownames = FALSE, filter = "top", selection = selection, escape = escape,
    class = "compact stripe hover nowrap",
    options = list(pageLength = page, scrollX = TRUE, lengthMenu = c(10, 25, 50, 100),
                   columnDefs = defs))
}

# Build one outreach row per founder from a founders-style or candidates-style CSV.
outreach_rows <- function(df) {
  if (is.null(df) || nrow(df) == 0) return(data.frame())
  startup <- first_col(df, c("startup_name"))
  url     <- first_col(df, c("website", "startup_url", "url"))
  ind     <- first_col(df, c("industry"))
  ctry    <- first_col(df, c("country"))
  fname   <- first_col(df, c("founder_name", "name"))
  email   <- first_col(df, c("founder_email", "email", "contact_email"))
  li      <- first_col(df, c("founder_linkedin", "linkedin_url", "linkedin"))
  data.frame(startup_name = startup, startup_url = url, industry = ind, country = ctry,
             founder_name = fname, email = email, linkedin = li, stringsAsFactors = FALSE)
}

render_tpl <- function(tpl, row) {
  repl <- list(
    "{founder_name}" = ifelse(nzchar(row$founder_name), row$founder_name, "there"),
    "{startup_name}" = ifelse(nzchar(row$startup_name), row$startup_name, "your company"),
    "{industry}"     = ifelse(nzchar(row$industry), row$industry, "your field"),
    "{country}"      = row$country, "{startup_url}" = row$startup_url,
    "{competition_name}" = INFO$competition_name, "{competition_year}" = as.character(INFO$competition_year),
    "{organizer_signature}" = INFO$organizer_signature, "{competition_url}" = INFO$competition_url)
  for (k in names(repl)) tpl <- gsub(k, repl[[k]], tpl, fixed = TRUE)
  tpl
}

# Reference outreach UI (send_email_app.html) embedded in the Outreach tab. The
# current run's CSV is injected so the self-contained app loads it on open; its
# drop-zone upload still works for loading a different file.
OUTREACH_TEMPLATE_HTML <- local({
  # Look in the app directory FIRST — that copy is bundled by rsconnect and works
  # when deployed to shinyapps.io; fall back to the pipeline root for local dev.
  candidates <- c(file.path(getwd(), "send_email_app.html"),
                  file.path(PROJECT, "send_email_app.html"))
  hit <- candidates[file.exists(candidates)]
  if (length(hit))
    tryCatch(paste(readLines(hit[[1]], warn = FALSE, encoding = "UTF-8"), collapse = "\n"),
             error = function(e) "")
  else ""
})

build_outreach_html <- function(csv_text) {
  if (!nzchar(OUTREACH_TEMPLATE_HTML))
    return("<p style='font:14px sans-serif;padding:20px'>send_email_app.html was not found next to the pipeline.</p>")
  # Set the CSV + SMTP info before the app script runs; auto-load once wired.
  smtp_info <- list(
    allow_live_send = isTRUE(INFO$allow_live_send),
    user = if (is.null(INFO$smtp_user)) "" else INFO$smtp_user,
    host = if (is.null(INFO$smtp_host)) "" else INFO$smtp_host)
  preload <- paste0("<script>window.PRELOADED_CSV = ",
                    jsonlite::toJSON(csv_text, auto_unbox = TRUE),
                    "; window.SMTP_INFO = ",
                    jsonlite::toJSON(smtp_info, auto_unbox = TRUE), ";</script>")
  runner <- paste0(
    "<script>(function(){if(!window.PRELOADED_CSV)return;try{",
    "var p=toObjects(parseCSV(window.PRELOADED_CSV));",
    "HEADERS=p.headers;ROWS=normalizeRows(p.headers,p.rows);selected.clear();",
    "if(HEADERS.includes(COL.name)&&ROWS.length){initFilters();",
    "document.getElementById('controls').style.display='block';",
    "var s=document.querySelector('#drop p strong');",
    "if(s)s.textContent='Loaded the current run \\u2014 drop another CSV to replace';",
    "render();}}catch(e){console.error(e);}})();</script>")
  html <- sub("<body>", paste0("<body>\n", preload), OUTREACH_TEMPLATE_HTML, fixed = TRUE)
  html <- sub("</body>", paste0(runner, "\n</body>"), html, fixed = TRUE)
  html
}

# ============================================================================ UI
# Brand styling to match the project website (SMU navy, Segoe UI, rounded cards).
app_css <- r"(
.navbar, header.navbar, .bslib-page-navbar > .navbar { background:#0f2f4c !important; }
.navbar-brand { color:#fff !important; font-weight:700; letter-spacing:.01em; }
h1,h2,h3,h4 { letter-spacing:-0.01em; }
a { color:#14507f; }
body, input, button, select, textarea, .form-control, .btn, .navbar-brand, .nav-link, .value-box-title, .value-box-value {
  font-family:'Segoe UI', -apple-system, BlinkMacSystemFont, Roboto, 'Helvetica Neue', Arial, sans-serif; }

.app-hero { background:linear-gradient(120deg,#0f2f4c 0%,#12406b 55%,#17537f 100%);
  color:#fff; padding:22px 26px; border-radius:14px; margin-bottom:18px;
  box-shadow:0 4px 14px rgba(15,47,76,.18); }
.app-hero-title { font-size:1.55rem; font-weight:800; line-height:1.15; }
.app-hero-sub { color:#d7e6f3; margin-top:6px; font-size:1rem; }
.app-hero-sub a { color:#bfe0ff; text-decoration:underline; }

.bslib-sidebar-layout > .sidebar { background:#f6f9fc; }
.sidebar label, .sidebar .form-label, .sidebar .control-label { font-weight:600; color:#2b3947; }
.sidebar hr { border-color:#dbe4ee; }

.bslib-value-box { border-radius:14px; border:1px solid #e3e7ee; box-shadow:0 1px 3px rgba(20,30,45,.06); }
.bslib-value-box .value-box-value { color:#12406b; font-weight:800; }
.bslib-value-box .value-box-title { color:#6b7686; text-transform:uppercase; letter-spacing:.03em; font-size:.8rem; }

.card, .bslib-card, .accordion, .accordion-item { border-radius:12px; }
.card { box-shadow:0 1px 3px rgba(20,30,45,.05); }

.nav-tabs { border-bottom:2px solid #eef2f7; }
.nav-tabs .nav-link { color:#5b6b7c; font-weight:600; border:none; }
.nav-tabs .nav-link.active { color:#12406b; background:transparent; border-bottom:3px solid #12406b; }

.btn-primary { background:#12406b; border-color:#12406b; border-radius:10px; font-weight:600; }
.btn-primary:hover, .btn-primary:focus { background:#0f3557; border-color:#0f3557; }
.btn-default, .btn-secondary, .btn-outline-secondary { border-radius:10px; }

table.dataTable thead th { background:#12406b !important; color:#fff !important; border:none !important; }
table.dataTable tbody tr:nth-child(even) { background:#f7fafc; }
.dataTables_wrapper .dataTables_length, .dataTables_wrapper .dataTables_filter { margin-bottom:10px; }
.dataTables_wrapper .dataTables_length label, .dataTables_wrapper .dataTables_filter label {
  display:inline-flex; align-items:center; gap:6px; font-weight:500; }
.dataTables_wrapper .dataTables_filter input,
.dataTables_wrapper .dataTables_length select {
  border:1px solid #cdd9e6; border-radius:8px; padding:4px 10px; height:auto; line-height:1.5;
  vertical-align:middle; -webkit-appearance:auto; -moz-appearance:auto; appearance:auto;
  background-image:none; font-size:0.9rem; }
.dataTables_wrapper .dataTables_length select { min-width:70px; padding-right:24px; }
.dataTables_wrapper .dataTables_paginate .paginate_button.current {
  background:#12406b !important; color:#fff !important; border:none !important; border-radius:6px; }

pre.shiny-text-output { background:#0f2f4c; color:#e6eef6; border-radius:10px; padding:12px 14px; font-size:12.5px; border:none; }
.local-only { background:#fff8e6; border:1px solid #f0e0b0; color:#6b5200; border-radius:10px; padding:10px 14px; font-size:13.5px; margin-bottom:12px; }
.waterfall-bar { margin:4px 0 12px; }
.waterfall-bar span { color:#5b6b7c; font-size:13px; }
)"

ui <- page_sidebar(
  title = "University Startup Sourcing Agent",
  theme = bs_theme(
    version = 5, primary = "#12406b",
    "font-family-sans-serif" = "'Segoe UI', -apple-system, BlinkMacSystemFont, Roboto, 'Helvetica Neue', Arial, sans-serif",
    "headings-font-weight" = "700", "border-radius" = "0.6rem"),
  sidebar = sidebar(
    width = 330, title = "Pipeline",
    selectInput("seed_file", "Seed list", choices = seed_files()),
    fileInput("seed_upload", "…or upload a seed CSV", accept = ".csv", width = "100%"),
    radioButtons("mode", "Mode", c("mock", "live"), inline = TRUE),
    selectInput("crawler", "Crawler", c("crawl4ai", "firecrawl")),
    selectInput("llm", "LLM backend", c("ollama", "google", "openai", "claude")),
    textInput("model", "LLM model", value = "qwen3:14b"),
    radioButtons("sel_mode", "Which seeds to run",
                 c("First N (by priority)" = "firstn", "Tick in Sources tab" = "pick")),
    conditionalPanel("input.sel_mode == 'firstn'",
      numericInput("max_sources", "Seed sources", 5, min = 1, max = 500),
      numericInput("start_idx", "Starting seed #", 1, min = 1)),
    numericInput("max_priority", "Max source priority", 2, min = 1, max = 3),
    numericInput("max_pages", "Pages per source", 2, min = 1, max = 20),
    hr(),
    tags$b("Contact waterfall"),
    checkboxInput("enrich", "Enable contact enrichment", FALSE),
    selectInput("search_backend", "Search", c("none", "brave")),
    selectInput("email_provider", "Email provider", c("none", "hunter")),
    selectInput("people_provider", "People provider", c("none", "apollo")),
    checkboxInput("verify", "Verify emails", FALSE),
    checkboxInput("paid_fallback", "Paid providers as fallback only", TRUE)
  ),
  tags$head(tags$style(HTML(app_css))),
  tags$head(tags$script(HTML(paste(
    "$(document).on('shiny:connected', function(){",
    "  Shiny.addCustomMessageHandler('outreach_smtp_result', function(msg){",
    "    var ifr = document.querySelector('#outreach_app iframe');",
    "    if(ifr && ifr.contentWindow){ ifr.contentWindow.postMessage({__smtp_result: msg}, '*'); }",
    "  });",
    "});", sep = "\n")))),
  navset_tab(
    nav_panel("1. Sources",
      layout_columns(col_widths = c(4, 4, 4), fill = FALSE,
        value_box("Sources", textOutput("m_sources")),
        value_box("Countries", textOutput("m_countries")),
        value_box("Priority 1", textOutput("m_pri1"))),
      helpText("In 'Tick in Sources tab' mode, select the rows you want to run."),
      DTOutput("seed_table"),
      textOutput("sel_count")),
    nav_panel("2. Run",
      if (!CAN_RUN) div(class = "local-only",
        tags$b("Viewer mode. "),
        "Running the sourcing pipeline needs the local Python environment (Ollama, Crawl4AI, provider APIs), ",
        "which this hosted app doesn't have. Run the pipeline on your machine, then browse and do outreach with the results here."),
      actionButton("run", "Run sourcing pipeline", class = "btn-primary",
                   disabled = if (!CAN_RUN) NA else NULL),
      actionButton("stop_run", "⏹ Stop & export partial", class = "btn-outline-secondary"),
      actionButton("refresh_run", "Refresh"),
      br(), br(), verbatimTextOutput("run_status"),
      tags$b("Live log"), verbatimTextOutput("run_log"),
      tags$b("Summary"), verbatimTextOutput("run_summary")),
    nav_panel("3. Candidates",
      fileInput("cand_upload", "Upload a candidates CSV (candidates_ranked / candidates_enriched) to view or enrich",
                accept = ".csv", width = "100%"),
      if (CAN_RUN) div(class = "waterfall-bar",
        actionButton("run_waterfall", "Run contact waterfall on the uploaded CSV", class = "btn-primary"),
        span("  — finds founder / company emails via the free-first waterfall (uses the sidebar Search / Email / Verify settings)"))
      else div(class = "local-only", tags$b("Viewer mode. "),
        "The contact waterfall runs locally only (it needs Python + provider APIs). Upload a CSV to browse it here; run the waterfall on your machine."),
      verbatimTextOutput("waterfall_status"),
      uiOutput("out_dir_ui"), DTOutput("cand_table")),
    nav_panel("4. Founders", DTOutput("founder_table")),
    nav_panel("5. Contact evidence", DTOutput("evidence_table")),
    nav_panel("6. Outreach", uiOutput("outreach_app"))
  )
)

# ============================================================================ SERVER
server <- function(input, output, session) {

  # ---- update model default when backend changes ----
  observeEvent(input$llm, {
    def <- switch(input$llm, ollama = "qwen3:14b", google = "gemini-2.5-flash",
                  openai = "gpt-5.5", claude = "", "")
    updateTextInput(session, "model", value = def)
  })

  # ---- Sources ----
  seeds <- reactive({
    if (!is.null(input$seed_upload))
      suppressWarnings(readr::read_csv(input$seed_upload$datapath, show_col_types = FALSE,
                                       col_types = readr::cols(.default = "c")))
    else { req(input$seed_file); read_seed(input$seed_file) }
  })
  output$m_sources   <- renderText(nrow(seeds()))
  output$m_countries <- renderText(if ("Country" %in% names(seeds())) length(unique(seeds()$Country)) else 0)
  output$m_pri1      <- renderText(sum(seeds()[["Priority (1=highest)"]] == "1", na.rm = TRUE))

  output$seed_table <- renderDT({
    df <- as.data.frame(seeds(), check.names = FALSE, stringsAsFactors = FALSE)
    # Replace the ID column with a 1-based index and drop Region for display.
    df[["ID"]] <- NULL
    df[["Region"]] <- NULL
    df <- data.frame(`#` = seq_len(nrow(df)), df, check.names = FALSE,
                     stringsAsFactors = FALSE)
    hide <- c("Crawl Focus", "Suggested Keywords / Paths", "Suggested Crawl Frequency",
              "Eligibility Signals to Extract", "Contact Strategy")
    hide_idx <- which(names(df) %in% hide) - 1  # 0-based (rownames = FALSE)
    extra <- if (length(hide_idx) > 0) list(list(visible = FALSE, targets = hide_idx)) else NULL
    # Render Seed URL as a clickable link; escape every other column.
    esc <- seq_along(df)
    if ("Seed URL" %in% names(df)) {
      u <- df[["Seed URL"]]
      df[["Seed URL"]] <- ifelse(
        is.na(u) | trimws(u) == "", "",
        paste0('<a href="', htmltools::htmlEscape(u, attribute = TRUE),
               '" target="_blank" rel="noopener">',
               htmltools::htmlEscape(u), '</a>'))
      esc <- setdiff(esc, which(names(df) == "Seed URL"))
    }
    web_dt(df, page = 15, selection = "multiple", escape = esc, extra_defs = extra)
  })
  selected_ids <- reactive({
    if (input$sel_mode != "pick") return(NULL)
    rows <- input$seed_table_rows_selected
    if (length(rows) == 0 || !("ID" %in% names(seeds()))) return(character(0))
    seeds()$ID[rows]
  })
  output$sel_count <- renderText({
    if (input$sel_mode == "pick") sprintf("%d seed(s) ticked.", length(selected_ids())) else ""
  })

  # ---- Run pipeline (background Python process) ----
  STOP_FILE <- file.path(RUN_OUT, "_stop.flag")
  rv <- reactiveValues(proc = NULL, log = NULL, running = FALSE, status = "Idle.", start = NULL)

  observeEvent(input$run, {
    if (!CAN_RUN) { rv$status <- "Viewer mode — run the pipeline in your local environment."; return() }
    if (input$sel_mode == "pick" && length(selected_ids()) == 0) {
      rv$status <- "Selection mode is on but no seeds are ticked."; return()
    }
    dir.create(RUN_OUT, showWarnings = FALSE, recursive = TRUE)
    if (file.exists(STOP_FILE)) file.remove(STOP_FILE)   # clear a stale stop request
    logpath <- file.path(RUN_OUT, "_shiny_run.log")
    if (file.exists(logpath)) file.remove(logpath)
    if (file.exists(file.path(RUN_OUT, "_shiny_summary.json"))) file.remove(file.path(RUN_OUT, "_shiny_summary.json"))
    seed_csv <- if (!is.null(input$seed_upload)) input$seed_upload$datapath else file.path(DATA, input$seed_file)
    cfg <- list(project = PROJECT, seed_csv = seed_csv, stop_file = STOP_FILE,
                output_dir = RUN_OUT, log = logpath, mode = input$mode,
                crawler_backend = input$crawler, llm_backend = input$llm,
                ollama_model = if (input$llm == "ollama") input$model else "",
                google_model = if (input$llm == "google") input$model else "",
                selected_ids = if (is.null(selected_ids())) NULL else as.list(selected_ids()),
                max_sources = input$max_sources, seed_start_index = input$start_idx,
                max_priority = input$max_priority, max_pages = input$max_pages,
                enrich = input$enrich, search_backend = input$search_backend,
                email_provider = input$email_provider, people_provider = input$people_provider,
                verify = input$verify, paid_fallback_only = input$paid_fallback)
    cfgpath <- file.path(RUN_OUT, "_shiny_cfg.json")
    write(jsonlite::toJSON(cfg, auto_unbox = TRUE, null = "null"), cfgpath)
    rv$proc <- processx::process$new(PY, c(file.path(PYDIR, "run.py"), cfgpath), wd = PROJECT)
    rv$log <- logpath; rv$running <- TRUE; rv$start <- Sys.time(); rv$status <- "Running ..."
  })

  # Graceful stop: request the pipeline to halt and export what it has so far.
  observeEvent(input$stop_run, {
    if (!isTRUE(rv$running)) { rv$status <- "No run in progress."; return() }
    writeLines("stop", STOP_FILE)
    rv$status <- "Stopping — finishing the current step, then writing partial results ..."
  })

  observe({
    if (isTRUE(rv$running)) {
      invalidateLater(1500)
      alive <- tryCatch(rv$proc$is_alive(), error = function(e) FALSE)
      if (!alive) {
        rv$running <- FALSE
        if (file.exists(STOP_FILE)) file.remove(STOP_FILE)
        sump <- file.path(RUN_OUT, "_shiny_summary.json")
        wall <- if (!is.null(rv$start)) as.numeric(difftime(Sys.time(), rv$start, units = "secs")) else NA
        if (file.exists(sump)) {
          s <- tryCatch(jsonlite::fromJSON(paste(readLines(sump, warn = FALSE), collapse = "")),
                        error = function(e) list())
          secs <- if (!is.null(s$elapsed_seconds)) s$elapsed_seconds else wall
          el <- if (!is.na(secs)) sprintf("  ⏱ %.1fs", secs) else ""
          rv$status <- if (isTRUE(s$stopped)) paste0("Stopped early — partial results written.", el)
                       else paste0("Complete.", el)
        } else {
          rv$status <- paste0("Stopped / failed.", if (!is.na(wall)) sprintf("  ⏱ %.0fs", wall) else "")
        }
      }
    }
  })
  runlog <- reactivePoll(1500, session,
    checkFunc = function() if (!is.null(rv$log) && file.exists(rv$log)) file.info(rv$log)$mtime else 0,
    valueFunc = function() if (!is.null(rv$log) && file.exists(rv$log))
      paste(tail(readLines(rv$log, warn = FALSE), 40), collapse = "\n") else "")
  output$run_status  <- renderText(rv$status)
  output$run_log     <- renderText(runlog())
  output$run_summary <- renderText({
    input$refresh_run; runlog()
    p <- file.path(RUN_OUT, "_shiny_summary.json")
    if (file.exists(p)) paste(readLines(p, warn = FALSE), collapse = "\n") else "(no summary yet)"
  })

  # ---- Contact waterfall on an uploaded CSV (local only) ----
  WF_OUT <- file.path(OUTROOT, "shiny_waterfall")
  rv_wf <- reactiveValues(proc = NULL, log = NULL, running = FALSE, status = "", out = NULL)

  observeEvent(input$run_waterfall, {
    if (!CAN_RUN) { rv_wf$status <- "Viewer mode — the contact waterfall runs in your local environment."; return() }
    if (is.null(input$cand_upload)) { rv_wf$status <- "Upload a candidates CSV first."; return() }
    dir.create(WF_OUT, showWarnings = FALSE, recursive = TRUE)
    logp <- file.path(WF_OUT, "_wf.log")
    if (file.exists(logp)) file.remove(logp)
    if (file.exists(file.path(WF_OUT, "_shiny_summary.json"))) file.remove(file.path(WF_OUT, "_shiny_summary.json"))
    cfg <- list(project = PROJECT, candidates_csv = input$cand_upload$datapath, output_dir = WF_OUT,
                log = logp, mode = input$mode, crawler_backend = input$crawler,
                search_backend = input$search_backend, email_provider = input$email_provider,
                people_provider = input$people_provider, verify = input$verify,
                paid_fallback_only = input$paid_fallback)
    cfgp <- file.path(WF_OUT, "_wf_cfg.json")
    write(jsonlite::toJSON(cfg, auto_unbox = TRUE, null = "null"), cfgp)
    rv_wf$proc <- processx::process$new(PY, c(file.path(PYDIR, "waterfall.py"), cfgp), wd = PROJECT)
    rv_wf$log <- logp; rv_wf$out <- WF_OUT; rv_wf$running <- TRUE; rv_wf$status <- "Running contact waterfall ..."
  })
  observe({
    if (isTRUE(rv_wf$running)) {
      invalidateLater(1500)
      alive <- tryCatch(rv_wf$proc$is_alive(), error = function(e) FALSE)
      if (!alive) { rv_wf$running <- FALSE
        rv_wf$status <- if (file.exists(file.path(WF_OUT, "_shiny_summary.json")))
          "Contact waterfall complete — enriched candidates shown below." else "Waterfall stopped / failed (see log)." }
    }
  })
  wflog <- reactivePoll(1500, session,
    checkFunc = function() if (!is.null(rv_wf$log) && file.exists(rv_wf$log)) file.info(rv_wf$log)$mtime else 0,
    valueFunc = function() if (!is.null(rv_wf$log) && file.exists(rv_wf$log))
      paste(tail(readLines(rv_wf$log, warn = FALSE), 20), collapse = "\n") else "")
  output$waterfall_status <- renderText({
    s <- rv_wf$status; l <- wflog()
    if (!nzchar(s) && !nzchar(l)) return("")
    paste(c(s, if (nzchar(l)) c("", l)), collapse = "\n")
  })

  # ---- Result viewers ----
  output$out_dir_ui <- renderUI({
    input$refresh_run; rv$running; rv_wf$running
    dirs <- output_dirs()
    sel <- if (RUN_OUT %in% dirs) RUN_OUT else if (length(dirs)) dirs[[1]] else NULL
    selectInput("out_dir", "Output run", choices = dirs, selected = sel, width = "100%")
  })
  res_df <- function(names) { req(input$out_dir); read_out(input$out_dir, names) }
  render_tbl <- function(df) { if (is.null(df)) DT::datatable(data.frame(Note = "No file in this run."),
      rownames = FALSE) else web_dt(df, page = 15) }
  output$cand_table <- renderDT({
    input$refresh_run; rv_wf$running
    # 1) enriched output from a just-finished waterfall run
    if (!is.null(rv_wf$out) && !isTRUE(rv_wf$running)) {
      wf <- read_out(rv_wf$out, c("candidates_enriched.csv"))
      if (!is.null(wf)) return(web_dt(wf, page = 15))
    }
    # 2) an uploaded candidates CSV, viewed as-is
    if (!is.null(input$cand_upload)) {
      up <- tryCatch(suppressWarnings(readr::read_csv(input$cand_upload$datapath, show_col_types = FALSE,
              col_types = readr::cols(.default = "c"))), error = function(e) NULL)
      if (!is.null(up)) return(web_dt(up, page = 15))
    }
    # 3) the selected output run
    render_tbl(res_df(c("candidates_ranked.csv", "candidates_enriched.csv")))
  })
  output$founder_table  <- renderDT(render_tbl(res_df(c("founders.csv"))))
  output$evidence_table <- renderDT(render_tbl(res_df(c("contact_evidence.csv"))))

  # ---- Outreach (embedded send_email_app.html, auto-loaded with the run CSV) ----
  output$outreach_app <- renderUI({
    input$refresh_run; rv$running
    dir <- input$out_dir
    if (is.null(dir)) {
      d <- output_dirs(); dir <- if (RUN_OUT %in% d) RUN_OUT else if (length(d)) d[[1]] else NULL
    }
    csv_text <- ""
    if (!is.null(dir)) {
      for (n in c("candidates_ranked.csv", "candidates_enriched.csv", "founders.csv")) {
        p <- file.path(dir, n)
        if (file.exists(p)) {
          csv_text <- paste(readLines(p, warn = FALSE, encoding = "UTF-8"), collapse = "\n"); break
        }
      }
    }
    tags$iframe(
      srcdoc = build_outreach_html(csv_text),
      style = "width:100%; height:1300px; border:1px solid #e3e7ee; border-radius:10px;")
  })

  # Automatic SMTP send requested from the embedded outreach app.
  observeEvent(input$outreach_smtp_send, {
    p <- tryCatch(jsonlite::fromJSON(input$outreach_smtp_send$payload, simplifyDataFrame = FALSE),
                  error = function(e) NULL)
    msgs <- if (is.null(p)) NULL else p$messages
    if (is.null(msgs) || length(msgs) == 0) {
      session$sendCustomMessage("outreach_smtp_result", list(error = "No messages to send.")); return()
    }
    clean <- lapply(msgs, function(m) list(to = m$to, subject = m$subject, body = m$body))
    outp <- file.path(tempdir(), "shiny_send_out.json")
    cfgp <- file.path(tempdir(), "shiny_send_cfg.json")
    write(jsonlite::toJSON(list(project = PROJECT, out = outp, messages = clean), auto_unbox = TRUE), cfgp)
    status <- tryCatch(
      system2(PY, c(shQuote(file.path(PYDIR, "send.py")), shQuote(cfgp)), stdout = TRUE, stderr = TRUE),
      error = function(e) conditionMessage(e))
    res <- tryCatch(
      jsonlite::fromJSON(paste(readLines(outp, warn = FALSE), collapse = ""), simplifyDataFrame = FALSE),
      error = function(e) NULL)
    if (is.null(res)) {
      session$sendCustomMessage("outreach_smtp_result",
        list(error = paste("Send failed:", paste(status, collapse = " "))))
    } else if (length(res) == 1 && !is.null(res[[1]]$status) && res[[1]]$status == "connect_error") {
      session$sendCustomMessage("outreach_smtp_result",
        list(error = paste("SMTP connection failed:", res[[1]]$error)))
    } else {
      session$sendCustomMessage("outreach_smtp_result", list(results = res))
    }
  })
}

shinyApp(ui, server)
