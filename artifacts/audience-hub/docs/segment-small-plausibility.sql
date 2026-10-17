-- Independent SQL references; NOT emitted by the segment compiler.
-- Bind :as_of to DATE '2024-12-31' using SQLAlchemy.
-- Run ONLY inside the isolated benchmark schema/role, never on live data.
-- Exact deterministic expected count is the closed range [result, result].

-- lapsed-high-value: over 730 days since last gift and lifetime giving >=500.
SELECT count(*) FROM active_profiles p JOIN profile_traits t ON t.profile_id=p.id
WHERE t.donor_status='lapsed' AND t.ltv_total>=500
AND p.id IN (SELECT profile_id FROM gifts WHERE gift_date<=:as_of
 GROUP BY profile_id HAVING max(gift_date)<CAST(:as_of AS date)-730 AND sum(amount)>=500);

-- new-partners: first gift in 90 inclusive UTC calendar dates.
SELECT count(*) FROM active_profiles p JOIN profile_traits t ON t.profile_id=p.id
WHERE t.first_gift_date BETWEEN CAST(:as_of AS date)-89 AND :as_of
AND p.id IN (SELECT profile_id FROM gifts WHERE gift_date<=:as_of
 GROUP BY profile_id HAVING min(gift_date)>=CAST(:as_of AS date)-89);

-- recurring-partners: recurring gift in 45 inclusive calendar dates.
SELECT count(*) FROM active_profiles p JOIN profile_traits t ON t.profile_id=p.id
WHERE t.is_recurring_active AND EXISTS (SELECT 1 FROM gifts g WHERE g.profile_id=p.id
 AND g.is_recurring AND g.gift_date BETWEEN CAST(:as_of AS date)-44 AND :as_of);

-- top-giving: positive raw 365-day giving, top ceil(N*0.05) with boundary ties.
WITH totals AS (
 SELECT p.id, sum(g.amount) AS amount FROM active_profiles p
 JOIN gifts g ON g.profile_id=p.id
 WHERE g.gift_date BETWEEN CAST(:as_of AS date)-364 AND :as_of
 GROUP BY p.id HAVING sum(g.amount)>0
), ranked AS (
 SELECT id, amount, rank() OVER(ORDER BY amount DESC) AS position,
 count(*) OVER() AS population FROM totals
)
SELECT count(*) FROM ranked r JOIN profile_traits t ON t.profile_id=r.id
WHERE r.position<=ceil(r.population*0.05) AND t.gift_amount_12m=r.amount;

-- engaged-not-giving: at least 3 actual Video Watched events, no gifts.
SELECT count(*) FROM active_profiles p JOIN profile_traits t ON t.profile_id=p.id
WHERE t.video_views_30d>=3 AND t.gift_count_total=0
AND NOT EXISTS (SELECT 1 FROM gifts g WHERE g.profile_id=p.id AND g.gift_date<=:as_of)
AND (SELECT count(*) FROM events e WHERE e.profile_id=p.id AND e.name='Video Watched'
 AND e.occurred_at>=CAST(:as_of AS date)-29
 AND e.occurred_at<CAST(:as_of AS date)+1)>=3;

-- lapsing-opted-in: explicit email opt-in, >=2 gifts, all older than 365 days.
SELECT count(*) FROM active_profiles p JOIN profile_traits t ON t.profile_id=p.id
WHERE t.gift_count_12m=0 AND t.gift_count_total>=2
AND EXISTS (SELECT 1 FROM consents c WHERE c.profile_id=p.id
 AND c.channel='email' AND c.status='opted_in')
AND p.id IN (SELECT profile_id FROM gifts WHERE gift_date<=:as_of
 GROUP BY profile_id HAVING count(*)>=2 AND max(gift_date)<CAST(:as_of AS date)-364);
