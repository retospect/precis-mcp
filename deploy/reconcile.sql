create sequence "public"."dream_log_attempt_id_seq";

create sequence "public"."patent_watches_id_seq";

create sequence "public"."ref_events_event_id_seq";

alter table "public"."artifact_kinds" drop constraint "artifact_kinds_target_check";

create table "public"."dream_log" (
    "attempt_id" bigint not null default nextval('dream_log_attempt_id_seq'::regclass),
    "created_at" timestamp with time zone not null default now(),
    "outcome" text not null,
    "behaviors" text[],
    "seed_clusters" jsonb,
    "result_ref_ids" bigint[],
    "turns" integer,
    "tool_calls" integer,
    "model" text,
    "cost_usd" double precision,
    "summary" jsonb
);


create table "public"."dream_transcripts" (
    "attempt_id" bigint not null,
    "transcript" jsonb not null
);


create table "public"."patent_watches" (
    "id" bigint not null default nextval('patent_watches_id_seq'::regclass),
    "name" text not null,
    "cql" text not null,
    "interval_s" integer not null,
    "max_per_pass" integer,
    "last_run_at" timestamp with time zone,
    "last_seen_pn" text[],
    "created_at" timestamp with time zone not null default now(),
    "created_by" text not null
);


create table "public"."ref_events" (
    "event_id" bigint not null default nextval('ref_events_event_id_seq'::regclass),
    "ref_id" bigint not null,
    "ts" timestamp with time zone not null default now(),
    "source" text not null,
    "event" text not null,
    "payload" jsonb,
    "duration_ms" integer,
    "cost_usd" numeric
);


create table "public"."tag_embeddings" (
    "namespace" text not null,
    "value" text not null,
    "vector" vector(1024),
    "version" integer not null default 1,
    "embedder" text not null,
    "embedded_at" timestamp with time zone not null default now()
);


alter table "public"."chunks" add column "accesses" integer not null default 0;

alter table "public"."chunks" add column "last_dreamt" timestamp with time zone not null default now();

alter table "public"."chunks" add column "last_seen" timestamp with time zone not null default now();

alter sequence "public"."dream_log_attempt_id_seq" owned by "public"."dream_log"."attempt_id";

alter sequence "public"."patent_watches_id_seq" owned by "public"."patent_watches"."id";

alter sequence "public"."ref_events_event_id_seq" owned by "public"."ref_events"."event_id";

CREATE INDEX chunk_embeddings_vec_hnsw_idx ON public.chunk_embeddings USING hnsw (vector vector_cosine_ops) WHERE ((status = 'ok'::text) AND (vector IS NOT NULL));

CREATE INDEX chunks_dream_score_idx ON public.chunks USING btree (((last_seen - last_dreamt)) DESC);

CREATE INDEX dream_log_behaviors_gin_idx ON public.dream_log USING gin (behaviors);

CREATE INDEX dream_log_outcome_created_idx ON public.dream_log USING btree (outcome, created_at);

CREATE UNIQUE INDEX dream_log_pkey ON public.dream_log USING btree (attempt_id);

CREATE UNIQUE INDEX dream_transcripts_pkey ON public.dream_transcripts USING btree (attempt_id);

CREATE INDEX patent_watches_due_idx ON public.patent_watches USING btree (last_run_at NULLS FIRST);

CREATE UNIQUE INDEX patent_watches_name_key ON public.patent_watches USING btree (name);

CREATE UNIQUE INDEX patent_watches_pkey ON public.patent_watches USING btree (id);

CREATE UNIQUE INDEX ref_events_pkey ON public.ref_events USING btree (event_id);

CREATE INDEX ref_events_ref_id_ts_idx ON public.ref_events USING btree (ref_id, ts DESC);

CREATE INDEX ref_events_source_event_ts_idx ON public.ref_events USING btree (source, event, ts DESC);

CREATE UNIQUE INDEX tag_embeddings_pkey ON public.tag_embeddings USING btree (namespace, value);

CREATE INDEX tag_embeddings_vector_hnsw ON public.tag_embeddings USING hnsw (vector vector_cosine_ops);

alter table "public"."dream_log" add constraint "dream_log_pkey" PRIMARY KEY using index "dream_log_pkey";

alter table "public"."dream_transcripts" add constraint "dream_transcripts_pkey" PRIMARY KEY using index "dream_transcripts_pkey";

alter table "public"."patent_watches" add constraint "patent_watches_pkey" PRIMARY KEY using index "patent_watches_pkey";

alter table "public"."ref_events" add constraint "ref_events_pkey" PRIMARY KEY using index "ref_events_pkey";

alter table "public"."tag_embeddings" add constraint "tag_embeddings_pkey" PRIMARY KEY using index "tag_embeddings_pkey";

alter table "public"."dream_transcripts" add constraint "dream_transcripts_attempt_id_fkey" FOREIGN KEY (attempt_id) REFERENCES dream_log(attempt_id) not valid;

alter table "public"."dream_transcripts" validate constraint "dream_transcripts_attempt_id_fkey";

alter table "public"."patent_watches" add constraint "patent_watches_interval_s_check" CHECK ((interval_s > 0)) not valid;

alter table "public"."patent_watches" validate constraint "patent_watches_interval_s_check";

alter table "public"."patent_watches" add constraint "patent_watches_max_per_pass_check" CHECK (((max_per_pass IS NULL) OR (max_per_pass > 0))) not valid;

alter table "public"."patent_watches" validate constraint "patent_watches_max_per_pass_check";

alter table "public"."patent_watches" add constraint "patent_watches_name_key" UNIQUE using index "patent_watches_name_key";

alter table "public"."ref_events" add constraint "ref_events_ref_id_fkey" FOREIGN KEY (ref_id) REFERENCES refs(ref_id) ON DELETE CASCADE not valid;

alter table "public"."ref_events" validate constraint "ref_events_ref_id_fkey";

alter table "public"."artifact_kinds" add constraint "artifact_kinds_target_check" CHECK ((target = ANY (ARRAY['chunk'::text, 'ref'::text, 'link'::text, 'pdf'::text, 'tag'::text]))) not valid;

alter table "public"."artifact_kinds" validate constraint "artifact_kinds_target_check";

set check_function_bodies = off;

CREATE OR REPLACE FUNCTION public.bump_salience(ids bigint[])
 RETURNS void
 LANGUAGE sql
AS $function$
    UPDATE chunks SET last_seen = now(), accesses = accesses + 1
    WHERE chunk_id = ANY(ids);
$function$
;


