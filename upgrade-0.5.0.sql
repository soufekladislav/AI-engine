-- Fresh AI Engine 0.5.0: run on EXISTING Engine DB only after backup.
-- Preserves all job/credit/account/provider data and admin route values.
-- Allow the established Anthropic route to be selected for high-quality chat.
UPDATE model_routes SET capabilities=CONCAT(capabilities, ',chat')
 WHERE code='anthropic-quality'
 AND FIND_IN_SET('chat',REPLACE(capabilities,' ',''))=0;
UPDATE model_routes SET capabilities=CONCAT(capabilities, ',general')
 WHERE code='anthropic-quality'
 AND FIND_IN_SET('general',REPLACE(capabilities,' ',''))=0;
-- DO NOT enable mock-local test route. Verified local memory runs before provider routing.
