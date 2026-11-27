"""Peer protocol reply projection and confirmed submission bodies."""
def confirmed_message(job, speaker_id):
    if job.get('message_prepared') and job['next_speaker']==0 and speaker_id==job['user_id']:
        return job['objective']+job['shared_content']
    return None
