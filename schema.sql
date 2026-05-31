create table if not exists public.daily_nutrition (
    user_id bigint not null,
    date date not null,
    total_calories integer not null default 0,
    total_protein integer not null default 0,
    primary key (user_id, date)
);