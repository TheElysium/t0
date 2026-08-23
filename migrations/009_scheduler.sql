-- Migration 009 : ordonnancement du sondage (lot L6)
-- Ajoute raw.poll_window, le registre d'avancement du sondage périodique.
-- Lot L6 (garminator) -- ne pas modifier la DDL manuellement hors migration.

-- Contexte : raw.payload_seen (migration 002) répond à « ce CONTENU exact a-t-il déjà
-- été vu ? » -- une question de déduplication au niveau du payload, indépendante du
-- temps. Elle ne répond PAS à « ai-je déjà correctement sondé Garmin pour ce jour ? »,
-- qui est une question d'ORDONNANCEMENT : quelles fenêtres reste-t-il à interroger, et
-- lesquelles ont été interrompues par une panne au milieu d'un run. C'est cette
-- deuxième question que raw.poll_window résout.
CREATE TABLE raw.poll_window (
    endpoint            text        NOT NULL,
    window_date         date        NOT NULL,
    status              text        NOT NULL DEFAULT 'pending',
    attempts            integer     NOT NULL DEFAULT 0,
    last_attempted_at   timestamptz NULL,
    completed_at        timestamptz NULL,
    last_ingest_run_id  bigint      NULL REFERENCES raw.ingest_run (ingest_run_id),
    last_error          text        NULL,
    CONSTRAINT poll_window_pk PRIMARY KEY (endpoint, window_date),
    CONSTRAINT poll_window_status_ck
        CHECK (status IN ('pending', 'in_progress', 'done', 'failed'))
);

COMMENT ON TABLE raw.poll_window IS
  'Registre d''avancement du sondage périodique, une ligne par (endpoint, jour calendaire local de l''athlète -- cf. raw.api_response.scope_date). Clé d''identification choisie délibérément DIFFÉRENTE de raw.payload_seen : ici on identifie une FENÊTRE DE TRAVAIL planifiée, pas un contenu. (endpoint, window_date) est stable et connu AVANT même d''interroger Garmin (contrairement à un hash de payload, qui suppose déjà d''avoir reçu la réponse) -- c''est ce qui permet de savoir, avant tout appel réseau, quelles fenêtres restent dues. RÈGLE D''IDEMPOTENCE : une fenêtre passe à ''in_progress'' AVANT l''appel réseau et à ''done'' seulement APRÈS son succès complet ; un crash au milieu la laisse ''in_progress'', jamais ''done'' -- l''ordonnanceur (garminator.scheduler) traite toute ligne ''in_progress'' plus vieille qu''un seuil de péremption comme un run interrompu à rejouer. Ainsi une panne ne peut jamais faire perdre une activité : au pire une fenêtre est resondée (ce que raw.payload_seen rend ensuite peu coûteux à détecter en aval). Une fenêtre ''done'' peut légitimement redevenir due : l''ordonnanceur ressonde toujours une fenêtre glissante récente pour rattraper les activités renommées, corrigées ou arrivées en retard côté Garmin -- l''idempotence ne doit jamais empêcher une réingestion légitime d''une version plus récente.';
COMMENT ON COLUMN raw.poll_window.endpoint IS
  'Nom de la méthode garminconnect sondée (même vocabulaire que raw.api_response.endpoint).';
COMMENT ON COLUMN raw.poll_window.window_date IS
  'Date LOCALE de l''athlète couverte par cette fenêtre de sondage (une fenêtre = un jour). Pas de granularité plus fine : les ~130 endpoints garminconnect utilisés ici sont journaliers (paramètre cdate).';
COMMENT ON COLUMN raw.poll_window.attempts IS
  'Nombre de tentatives de sondage de cette fenêtre, y compris l''échec le plus récent. Incrémenté à chaque passage à ''in_progress'', jamais remis à zéro : un compteur qui repart de zéro masquerait qu''une fenêtre a déjà nécessité de nombreux essais.';
COMMENT ON COLUMN raw.poll_window.last_error IS
  'Message de la dernière erreur si status = ''failed''. Remis à NULL dès que la fenêtre repasse ''done'' -- ne jamais laisser une erreur obsolète à côté d''un succès plus récent.';

CREATE INDEX poll_window_due_idx
    ON raw.poll_window (endpoint, status, window_date);

COMMENT ON INDEX raw.poll_window_due_idx IS
  'Sert la requête de planification : pour un endpoint donné, retrouver rapidement les fenêtres par statut et par date (les plus anciennes dues en premier, cf. garminator.scheduler.planning.due_windows).';
