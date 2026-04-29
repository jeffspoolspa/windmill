import wmill
from supabase import create_client


def main(wo_number: str, subject: str):
    url = wmill.get_variable("f/SUPABASE/URL")
    key = wmill.get_variable("f/SUPABASE/ANON_KEY")

    client = create_client(url,key)
    try:
        if "Acceptance" in subject:
            response = (
                client.table('estimates')
                .update({'approval_status': 'Accepted'})
                .eq('wo_number', wo_number)
                .execute()
            )
        elif "Declined" in subject:
            response = (
                client.table('estimates')
                .update({'approval_status': 'Declined', 'status': 'declined'})
                .eq('wo_number', wo_number)
                .execute()
            )
        
        if len(response.data) == 0:
            return {
                "success": False,
                "message": f"No estimate found with wo_number {wo_number}"
            }
        
        return {
            "success": True,
            "message": f"Approved estimate {wo_number}",
            "data": response.data
        }
        
    except Exception as e:
        return {
            "success": False,
            "message": f"Error: {str(e)}"
        }
