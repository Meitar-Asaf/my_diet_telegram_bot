create table if not exists public.daily_nutrition (
    user_id bigint not null,
    date date not null,
    total_calories integer not null default 0,
    total_protein integer not null default 0,
    primary key (user_id, date)
);

create table if not exists public.food_log (
    id serial primary key,
    user_id bigint not null,
    date date not null,
    description text not null,
    calories integer not null default 0,
    protein integer not null default 0,
    created_at timestamptz default now()
);

create index if not exists food_log_user_date on public.food_log(user_id, date);