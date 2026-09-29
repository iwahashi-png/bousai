create extension if not exists pgcrypto;

create table if not exists public.community_reports (
  id uuid primary key default gen_random_uuid(),
  reported_at timestamptz not null,
  category text not null,
  place text not null,
  latitude double precision not null check (latitude between -90 and 90),
  longitude double precision not null check (longitude between -180 and 180),
  reporter text not null,
  comment text not null default '',
  photo_key text,
  created_at timestamptz not null default now()
);

alter table public.community_reports enable row level security;
alter table public.community_reports drop constraint if exists community_reports_category_check;

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
  'community-report-photos',
  'community-report-photos',
  true,
  8388608,
  array['image/png']
)
on conflict (id) do update
set public = excluded.public,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;